"""Tests for the container-record storage primitives (design §2)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from thoughtmachine.container_record import storage
from thoughtmachine.container_record.models import RecordCorrupt, RecordLocked


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


def test_path_shapes(vault):
    ws, rid = "ws-1", "rec-1"
    assert storage.containers_dir(ws, vault) == vault / "workspaces" / ws / "containers"
    assert (
        storage.record_path(ws, rid, vault)
        == vault / "workspaces" / ws / "containers" / "rec-1.json"
    )
    # Doc §2.3 verbatim: the lock is ``<id>.json.lock``.
    assert (
        storage.lock_path(ws, rid, vault)
        == vault / "workspaces" / ws / "containers" / "rec-1.json.lock"
    )
    assert storage.events_path(ws, rid, vault).name == "rec-1.events.jsonl"
    assert storage.events_path(ws, rid, vault).parent == storage.containers_dir(ws, vault)
    assert storage.migrations_log_path(ws, vault).name == "migrations.log"
    assert storage.migrations_log_path(ws, vault).parent == storage.containers_dir(ws, vault)


def test_resolve_vault_root_honours_env(vault):
    assert storage.resolve_vault_root() == vault.resolve()
    assert storage.resolve_vault_root("/explicit") == Path("/explicit")


def test_write_read_roundtrip_and_mode(vault):
    path = storage.record_path("ws", "r1", vault)
    data = {"id": "r1", "schema_version": 1, "event_log": []}
    storage.write_record_file(path, data)
    assert storage.read_record_file(path) == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_read_missing_returns_none(vault):
    assert storage.read_record_file(storage.record_path("ws", "nope", vault)) is None


def test_corrupt_is_quarantined_and_raises(vault):
    path = storage.record_path("ws", "bad", vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(RecordCorrupt):
        storage.read_record_file(path)

    assert not path.exists()
    leftovers = [p.name for p in path.parent.iterdir()]
    assert any(name.startswith("bad.json.corrupt-") for name in leftovers)


def test_lock_timeout_raises_record_locked(vault):
    fcntl = pytest.importorskip("fcntl")
    lock = storage.lock_path("ws", "r1", vault)
    lock.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RecordLocked):
            with storage.record_lock(lock, timeout=0.2, poll_interval=0.02):
                pass
    finally:
        os.close(held)


def test_lock_acquirable_after_release(vault):
    lock = storage.lock_path("ws", "r1", vault)
    with storage.record_lock(lock, timeout=1.0):
        pass
    with storage.record_lock(lock, timeout=1.0):
        pass


def test_iter_record_files_filters_sidecars_locks_and_corrupt(vault):
    containers = storage.containers_dir("ws", vault)
    containers.mkdir(parents=True)
    (containers / "a.json").write_text("{}", encoding="utf-8")
    (containers / "b.json").write_text("{}", encoding="utf-8")
    (containers / "a.json.lock").write_text("", encoding="utf-8")
    (containers / "a.events.jsonl").write_text("\n", encoding="utf-8")
    (containers / "c.json.corrupt-20200101000000000000").write_text("{}", encoding="utf-8")
    (containers / "migrations.log").write_text("", encoding="utf-8")

    assert sorted(p.name for p in storage.iter_record_files(containers)) == ["a.json", "b.json"]


def test_append_event_line_and_sidecar(vault):
    sidecar = storage.events_path("ws", "r1", vault)
    storage.append_event_line(
        sidecar, {"timestamp": "t", "event_type": "x", "actor": "a", "payload": {}}
    )
    storage.append_event_line(
        sidecar, {"timestamp": "t", "event_type": "y", "actor": "a", "payload": {}}
    )
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event_type"] for line in lines] == ["x", "y"]

    storage.write_events_sidecar(sidecar, [{"event_type": "z"}])
    assert [
        json.loads(line)["event_type"]
        for line in sidecar.read_text(encoding="utf-8").splitlines()
    ] == ["z"]


def test_read_events_sidecar(vault):
    sidecar = storage.events_path("ws", "r1", vault)
    assert storage.read_events_sidecar(sidecar) == []
    storage.append_event_line(sidecar, {"event_type": "x"})
    storage.append_event_line(sidecar, {"event_type": "y"})
    assert [e["event_type"] for e in storage.read_events_sidecar(sidecar)] == ["x", "y"]


def test_append_migration_line(vault):
    log = storage.migrations_log_path("ws", vault)
    storage.append_migration_line(log, {"docker_id": "d1", "record_id": "r1", "ts": "t"})
    storage.append_migration_line(log, {"docker_id": "d2", "record_id": "r2", "ts": "t"})
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["record_id"] == "r2"


def test_iter_workspace_ids(vault):
    for ws in ("ws-b", "ws-a"):
        storage.containers_dir(ws, vault).mkdir(parents=True)
    (vault / "workspaces" / "no-containers").mkdir(parents=True)
    assert storage.iter_workspace_ids(vault) == ["ws-a", "ws-b"]
