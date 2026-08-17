"""Retention scenarios: end-to-end acceptance tests for the vault GC.

These tests exercise the age-based retention behaviour of
``thoughtmachine.vault_gc.run_gc`` (and the ``delete_workspace`` teardown it
delegates to) against a hermetic vault, using the shared fakes from
``tests.test_vault_gc`` and ``tests.test_workspace_lifecycle``.

Scenarios covered
-----------------
01. An ACTIVE workspace (last activity inside the 7-day active window) is
    retained by the stale-workspaces sweep.
02. A STALE workspace (no activity for > 90 days, not active) is removed
    end-to-end: the workspace vault dir is deleted and the registry entry is
    unregistered.
03. Activity-based retention: a workspace whose last activity is recent is
    retained even though it was created long ago, and a moderately stale
    workspace (40 days) is retained because it is below the 90-day stale
    threshold.
04. Orphan session files: old non-open session files are removed; an old but
    OPEN session is protected; a fresh session is kept.
05. Containers: stopped old containers are removed (resource + orphaned user
    containers); running containers and containers of registered workspaces
    are never touched.
06. Dry-run mode: ``run_gc(dry_run=True)`` and ``delete_workspace(dry_run=True)``
    report every eligible item under ``would_remove`` and mutate nothing.
07. Orphan workspace dirs: an unregistered, old directory is removed; a
    registered directory and a fresh directory are kept; symlinks are refused.
08. Volumes: a ``tm-packages-*`` volume of an unregistered workspace is
    removed; volumes of registered workspaces and non-package volumes are
    kept.
09. The read-only API surface (registry list, session-store reads, knowledge
    base reads, working-document reads) creates no files on disk.
10. A PINNED workspace (``metadata.pinned`` truthy on the registry entry) is
    retained by the stale-workspaces sweep even when stale.
11. A stale workspace with an OPEN SESSION referencing it is retained.
12. A stale workspace with an IN-USE (running/paused) container is retained.

Retention guards (all read-only, checked before any removal): the active
window, pinned metadata (``metadata.pinned`` on the registry entry), open
sessions referencing the workspace, and in-use (running/paused/restarting)
containers for the workspace.
"""

import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agent.knowledge.global_kb as global_kb
import infra.container_manager as cm  # noqa: F401  (patched via test_workspace_lifecycle helpers)
import thoughtmachine.vault
import thoughtmachine.vault_gc as gc
import thoughtmachine.workspace_lifecycle as wl

from session.store import FileSystemSessionStore
from thoughtmachine.workspace_capabilities import ensure_workspace_dirs
from thoughtmachine.workspace_registry import WorkspaceRegistry
from tools.knowledge_base import KnowledgeBaseTool
from tools.workspace.working_document import WorkingDocument

from tests.test_vault_gc import (  # noqa: F401
    NOW,
    OLD,
    STALE,
    FRESH,
    RECENT,
    _iso,
    FakeClient,
    FakeContainer,
    FakeEntry,
    FakeRegistry,
    FakeVolume,
    _make_ws_dir as gc_make_ws_dir,
    _write_session as gc_write_session,
)
from tests.test_workspace_lifecycle import (
    EXPECTED_STEPS,
    _make_workspace as wl_make_workspace,
    _patch_deps,
)

_GC_ENV_VARS = (
    "TM_GC_STALE_WORKSPACE_DAYS",
    "TM_GC_ORPHAN_WORKSPACE_DIR_DAYS",
    "TM_GC_ORPHAN_SESSION_DAYS",
    "TM_GC_ORPHAN_RESOURCE_CONTAINER_HOURS",
    "TM_GC_ORPHAN_VOLUME_DAYS",
    "TM_GC_ACTIVE_WINDOW_DAYS",
)


@pytest.fixture()
def vault_path(tmp_path, monkeypatch):
    """Hermetic vault: Path.home + vault_root patched into tmp_path."""
    for var in _GC_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    vault = tmp_path / ".thoughtmachine"
    (vault / "state").mkdir(parents=True, exist_ok=True)
    (vault / "sessions").mkdir(parents=True, exist_ok=True)
    (vault / "workspaces").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(thoughtmachine.vault, "vault_root", lambda: vault)
    monkeypatch.setattr(gc, "vault_root", lambda: vault)
    monkeypatch.setattr(wl, "vault_root", lambda: vault)
    return vault


def _fs_paths(root):
    """Set of relative path strings (files AND dirs) under *root*."""
    paths = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        paths.add(str(rel))
        for fn in filenames:
            paths.add(str(rel / fn))
    return paths


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── Scenario 01: active workspace retained ──────────────────────────────────


def test_01_active_workspace_is_retained(vault_path, monkeypatch):
    ws_id = "ws-active-01"
    ensure_workspace_dirs(ws_id)
    ws_dir = vault_path / "workspaces" / ws_id
    assert ws_dir.is_dir()

    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry(
            entries=[FakeEntry(id=ws_id, last_opened=_iso(FRESH), updated_at=_iso(OLD))]
        ),
        docker_client=FakeClient(containers=[], volumes=[]),
    )

    stale = report["categories"]["stale_workspaces"]
    assert stale["removed"] == []
    assert stale["skipped"] == [
        {"id": ws_id, "reason": "active (within active window)"}
    ]
    # The registered workspace dir is also protected by the orphan-dir sweep.
    assert ws_dir.is_dir()


# ── Scenario 02: stale workspace removed end-to-end ─────────────────────────


def test_02_stale_workspace_removed_end_to_end(vault_path, monkeypatch):
    ws_id = "ws-stale-02"
    wl_make_workspace(vault_path, ws_id=ws_id, with_session=True)
    ws_dir = vault_path / "workspaces" / ws_id
    assert (ws_dir / "sessions").is_dir()

    registry = _patch_deps(monkeypatch, tmp_path=vault_path.parent)

    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry(
            entries=[FakeEntry(id=ws_id, last_opened=_iso(OLD), updated_at=_iso(OLD))]
        ),
        docker_client=FakeClient(containers=[], volumes=[]),
    )

    stale = report["categories"]["stale_workspaces"]
    assert stale["removed"] == [ws_id]
    assert stale["errors"] == []
    # The full teardown ran: vault dir gone, registry entry unregistered.
    assert not ws_dir.exists()
    assert ws_id in registry.unregister_calls


# ── Scenario 03: activity-based retention ───────────────────────────────────


def test_03_recent_activity_retains_old_workspace(vault_path, monkeypatch):
    _patch_deps(monkeypatch, tmp_path=vault_path.parent)
    ws_old_but_active = "ws-old-but-active-03"
    ws_medium = "ws-medium-03"
    ws_truly_stale = "ws-truly-stale-03"

    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry(
            entries=[
                # Created long ago but touched recently -> retained (active).
                FakeEntry(
                    id=ws_old_but_active,
                    last_opened="",
                    updated_at=_iso(RECENT),
                    created_at=_iso(OLD),
                ),
                # 40 days of inactivity: past the window, below the 90d stale
                # threshold -> retained ("not old enough").
                FakeEntry(
                    id=ws_medium,
                    last_opened=_iso(STALE),
                    updated_at=_iso(STALE),
                    created_at=_iso(STALE),
                ),
                # 200 days of inactivity -> removed.
                FakeEntry(
                    id=ws_truly_stale,
                    last_opened=_iso(OLD),
                    updated_at=_iso(OLD),
                    created_at=_iso(OLD),
                ),
            ]
        ),
        docker_client=FakeClient(containers=[], volumes=[]),
    )

    stale = report["categories"]["stale_workspaces"]
    assert stale["removed"] == [ws_truly_stale]
    reasons = {s["id"]: s["reason"] for s in stale["skipped"]}
    assert reasons[ws_old_but_active] == "active (within active window)"
    assert reasons[ws_medium] == "not old enough"


# ── Scenario 04: orphan session files ───────────────────────────────────────


def test_04_orphan_session_retention(vault_path):
    gc_write_session(vault_path / "sessions", "old-sess", OLD)
    gc_write_session(vault_path / "sessions", "open-sess", OLD)
    gc_write_session(vault_path / "sessions", "fresh-sess", FRESH)
    _write_json(vault_path / "state" / "open_sessions.json", ["open-sess"])

    wl_make_workspace(vault_path, ws_id="ws-seeded-04", with_session=True)
    gc_write_session(
        vault_path / "workspaces" / "ws-seeded-04" / "sessions", "ws-sess", OLD
    )

    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry(entries=[]),
        docker_client=FakeClient(containers=[], volumes=[]),
    )

    sessions = report["categories"]["orphan_sessions"]
    removed = set(sessions["removed"])
    assert "old-sess" in removed
    assert "ws-sess" in removed
    reasons = {s["id"]: s["reason"] for s in sessions["skipped"]}
    assert reasons["open-sess"] == "session is open"
    assert reasons["fresh-sess"] == "not old enough"
    # Open + fresh files still on disk; _meta companions handled too.
    assert (vault_path / "sessions" / "open-sess.json").exists()
    assert (vault_path / "sessions" / "fresh-sess.json").exists()
    assert not (vault_path / "sessions" / "old-sess.json").exists()
    assert not (vault_path / "sessions" / "_meta_old-sess.json").exists()
    assert not (
        vault_path / "workspaces" / "ws-seeded-04" / "sessions" / "ws-sess.json"
    ).exists()


# ── Scenario 05: container retention ─────────────────────────────────────────


def test_05_container_retention(vault_path):
    containers = [
        FakeContainer(
            "res-old", status="exited", created=_iso(OLD),
            labels={"thoughtmachine.resource": "1"},
        ),
        FakeContainer(
            "res-running", status="running", created=_iso(OLD),
            labels={"thoughtmachine.resource": "1"},
        ),
        FakeContainer(
            "user-orphan", status="exited", created=_iso(OLD),
            labels={"thoughtmachine.workspace_id": "gone-ws-05"},
        ),
        FakeContainer(
            "user-registered", status="exited", created=_iso(OLD),
            labels={"thoughtmachine.workspace_id": "ws-reg-05"},
        ),
        FakeContainer(
            "user-running", status="running", created=_iso(OLD),
            labels={"thoughtmachine.workspace_id": "gone-ws-05"},
        ),
    ]
    client = FakeClient(containers=containers, volumes=[])

    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry(
            entries=[FakeEntry(id="ws-reg-05", last_opened=_iso(FRESH))]
        ),
        docker_client=client,
    )

    cat = report["categories"]["orphan_resource_containers"]
    assert sorted(cat["removed"]) == ["res-old", "user-orphan"]
    reasons = {s["id"]: s["reason"] for s in cat["skipped"]}
    assert reasons["res-running"] == "in use (status=running)"
    assert reasons["user-running"] == "in use (status=running)"
    assert reasons["user-registered"] == "belongs to a registered workspace"
    # Removed containers really were removed via the docker API.
    assert containers[0].remove_calls == [{"force": True}]  # res-old
    assert containers[2].remove_calls == [{"force": True}]  # user-orphan
    assert containers[4].remove_calls == []  # user-running


# ── Scenario 06: dry-run mutates nothing ─────────────────────────────────────


def test_06_dry_run_reports_without_mutating(vault_path, monkeypatch):
    ws_id = "ws-dry-06"
    wl_make_workspace(vault_path, ws_id=ws_id, with_session=True)
    ws_dir = vault_path / "workspaces" / ws_id
    _patch_deps(monkeypatch, tmp_path=vault_path.parent)

    report = gc.run_gc(
        dry_run=True,
        now=NOW,
        registry=FakeRegistry(
            entries=[FakeEntry(id=ws_id, last_opened=_iso(OLD))]
        ),
        docker_client=FakeClient(containers=[], volumes=[]),
    )

    assert report["dry_run"] is True
    assert report["categories"]["stale_workspaces"]["would_remove"] == [ws_id]
    assert report["categories"]["stale_workspaces"]["removed"] == []
    assert ws_dir.is_dir()  # nothing removed

    # delete_workspace(dry_run=True) likewise reports every step, mutates nothing.
    result = wl.delete_workspace(ws_id, dry_run=True)
    assert result["dry_run"] is True
    assert result["would_remove"] == list(EXPECTED_STEPS)
    assert result["removed"] == []
    assert result["errors"] == []
    assert ws_dir.is_dir()


# ── Scenario 07: orphan workspace dirs ───────────────────────────────────────


def test_07_orphan_workspace_dir_retention(vault_path):
    gc_make_ws_dir(vault_path, "orphan-old-07", mtime=OLD)
    gc_make_ws_dir(vault_path, "reg-old-07", mtime=OLD)
    gc_make_ws_dir(vault_path, "orphan-fresh-07", mtime=FRESH)
    link_target = vault_path.parent / "link-target-07"
    link_target.mkdir(exist_ok=True)
    os.symlink(link_target, vault_path / "workspaces" / "link-07")

    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry(
            entries=[FakeEntry(id="reg-old-07", last_opened=_iso(FRESH))]
        ),
        docker_client=FakeClient(containers=[], volumes=[]),
    )

    cat = report["categories"]["orphan_workspace_dirs"]
    assert cat["removed"] == [str(vault_path / "workspaces" / "orphan-old-07")]
    reasons = {s["id"]: s["reason"] for s in cat["skipped"]}
    assert reasons[str(vault_path / "workspaces" / "reg-old-07")] == (
        "registered workspace"
    )
    assert reasons[str(vault_path / "workspaces" / "orphan-fresh-07")] == (
        "not old enough"
    )
    assert reasons[str(vault_path / "workspaces" / "link-07")] == (
        "symlink (refusing)"
    )
    assert not (vault_path / "workspaces" / "orphan-old-07").exists()
    assert (vault_path / "workspaces" / "reg-old-07").exists()
    assert (vault_path / "workspaces" / "orphan-fresh-07").exists()
    assert (vault_path / "workspaces" / "link-07").is_symlink()


# ── Scenario 08: package volume retention ────────────────────────────────────


def test_08_volume_retention(vault_path):
    volumes = [
        FakeVolume("tm-packages-orphan-08", _iso(OLD)),
        FakeVolume("tm-packages-ws1-08", _iso(OLD)),
        FakeVolume("other-volume", _iso(OLD)),
    ]
    client = FakeClient(containers=[], volumes=volumes)

    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry(
            entries=[FakeEntry(id="ws1-08", last_opened=_iso(FRESH))]
        ),
        docker_client=client,
    )

    cat = report["categories"]["orphan_volumes"]
    assert cat["removed"] == ["tm-packages-orphan-08"]
    reasons = {s["id"]: s["reason"] for s in cat["skipped"]}
    assert reasons["tm-packages-ws1-08"] == "belongs to a registered workspace"
    # Non-package volume appears nowhere.
    all_ids = cat["removed"] + cat["would_remove"] + [s["id"] for s in cat["skipped"]]
    assert "other-volume" not in all_ids
    assert volumes[0].remove_calls == [{"force": True}]  # tm-packages-orphan-08
    assert volumes[1].remove_calls == []  # tm-packages-ws1-08


# ── Scenario 09: read-only APIs create no files ──────────────────────────────


def test_09_read_only_apis_create_no_files(vault_path, monkeypatch):
    # Workspace project root (outside the vault): knowledge + working docs.
    ws_dir = vault_path.parent / "ws-read-09"
    kb_file = ws_dir / ".thoughtmachine" / "knowledge" / "project" / "system_architecture.md"
    kb_file.parent.mkdir(parents=True, exist_ok=True)
    kb_file.write_text("# System Architecture\n\nmarker-kb-09\n", encoding="utf-8")
    doc_file = ws_dir / ".thoughtmachine" / "working_docs" / "doc1.json"
    doc_file.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        doc_file,
        {
            "doc_id": "doc1",
            "title": "doc one",
            "updated_at": _iso(FRESH),
            "sections": {"notes": "marker-doc-09"},
        },
    )
    # Global KB user domain file under the vault.
    global_user = vault_path / "global" / "user" / "testdomain-09.md"
    global_user.parent.mkdir(parents=True, exist_ok=True)
    global_user.write_text("marker-global-09\n", encoding="utf-8")
    # Re-point GLOBAL_KB_DIR at the patched home.
    importlib.reload(global_kb)
    monkeypatch.setattr(
        global_kb, "GLOBAL_KB_DIR", vault_path / "global"
    )

    registry_file = vault_path / "state" / "workspace_registry.json"
    _write_json(registry_file, [])

    before = _fs_paths(tmp_path_root := vault_path.parent)

    # Registry read.
    registry = WorkspaceRegistry(path=registry_file)
    assert registry.list_workspaces() == []

    # Session-store reads.
    store = FileSystemSessionStore(
        sessions_dir=str(vault_path / "sessions"),
        state_dir=str(vault_path / "state"),
    )
    assert store.list_sessions() == []
    assert store.get_current_session_id() is None

    # Knowledge-base reads (workspace + global scope).
    kb_ws = KnowledgeBaseTool(
        mode="read", domain="system_architecture", workspace_path=str(ws_dir)
    ).execute()
    assert "marker-kb-09" in kb_ws
    kb_global = KnowledgeBaseTool(
        mode="read", domain="testdomain-09", scope="global"
    ).execute()
    assert "marker-global-09" in kb_global

    # Working-document reads.
    wd_list = WorkingDocument(action="list", workspace_path=str(ws_dir)).execute()
    assert "doc1" in wd_list
    wd_read = WorkingDocument(
        action="read", doc_id="doc1", section="notes", workspace_path=str(ws_dir)
    ).execute()
    assert "marker-doc-09" in wd_read

    # Nothing was created or modified anywhere under tmp_path.
    assert _fs_paths(tmp_path_root) == before


# ── Scenario 10: pinned workspace retained ───────────────────────────────────────────────


class _FakeEntryWithMetadata(FakeEntry):
    """FakeEntry variant exposing a ``metadata`` dict (registry parity)."""

    def __init__(self, id, last_opened="", updated_at="", created_at="", metadata=None):
        super().__init__(
            id, last_opened=last_opened, updated_at=updated_at, created_at=created_at
        )
        self.metadata = dict(metadata or {})


def test_10_pinned_workspace_retained(vault_path, monkeypatch):
    _patch_deps(monkeypatch, tmp_path=vault_path.parent)
    entries = [
        _FakeEntryWithMetadata(
            id="ws-pinned-10", last_opened=_iso(OLD), metadata={"pinned": True}
        ),
        FakeEntry(id="ws-plain-10", last_opened=_iso(OLD)),
    ]
    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry(entries=entries),
        docker_client=FakeClient(containers=[], volumes=[]),
    )

    stale = report["categories"]["stale_workspaces"]
    assert stale["removed"] == ["ws-plain-10"]
    assert (
        {"id": "ws-pinned-10", "reason": "pinned (metadata.pinned)"} in stale["skipped"]
    )


# ── Scenario 11: open-session workspace retained ──────────────────────────────────


def test_11_workspace_with_open_session_retained(vault_path):
    ws_id = "ws-open-11"
    wl_make_workspace(vault_path, ws_id=ws_id, with_session=True)
    gc_write_session(
        vault_path / "workspaces" / ws_id / "sessions", "open-sess-11", OLD
    )
    _write_json(vault_path / "state" / "open_sessions.json", ["open-sess-11"])

    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry([FakeEntry(id=ws_id, last_opened=_iso(OLD))]),
        docker_client=FakeClient(containers=[], volumes=[]),
    )

    stale = report["categories"]["stale_workspaces"]
    assert stale["removed"] == []
    assert {"id": ws_id, "reason": "has open sessions"} in stale["skipped"]


# ── Scenario 12: in-use container workspace retained ───────────────────────────────────


def test_12_workspace_with_running_container_retained(vault_path):
    ws_id = "ws-running-12"
    containers = [
        FakeContainer(
            "user-running-12",
            status="running",
            created=_iso(OLD),
            labels={gc._WORKSPACE_LABEL: ws_id},
        ),
        FakeContainer(
            "res-paused-12",
            status="paused",
            created=_iso(OLD),
            labels={gc._WORKSPACE_LABEL: ws_id, gc._RESOURCE_LABEL: "1"},
        ),
    ]
    report = gc.run_gc(
        now=NOW,
        registry=FakeRegistry([FakeEntry(id=ws_id, last_opened=_iso(OLD))]),
        docker_client=FakeClient(containers=containers, volumes=[]),
    )

    stale = report["categories"]["stale_workspaces"]
    assert stale["removed"] == []
    assert {"id": ws_id, "reason": "has in-use containers"} in stale["skipped"]
    assert containers[0].remove_calls == []
