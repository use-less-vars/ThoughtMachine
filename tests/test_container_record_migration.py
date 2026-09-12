"""Tests for the container-record legacy migration (design §4)."""

from __future__ import annotations

import json

import pytest

from thoughtmachine.container_record import api, migration, storage
from thoughtmachine.container_record.models import SCHEMA_VERSION_LEGACY

WS = "ws-1"


class _FakeContainers:
    def __init__(self, payloads):
        self._payloads = payloads

    def list(self, all=False, filters=None):  # noqa: A002 - docker signature
        return list(self._payloads)


class FakeDockerClient:
    """Minimal stand-in for ``docker.DockerClient`` (§5.1)."""

    def __init__(self, payloads):
        self.containers = _FakeContainers(payloads)


def make_payload(
    docker_id,
    labels,
    *,
    network="none",
    mem=268435456,
    cpu=50000,
    oom=None,
    status="running",
    image="sha256:abc",
):
    return {
        "Id": docker_id,
        "Config": {"Labels": dict(labels)},
        "HostConfig": {
            "NetworkMode": network,
            "Memory": mem,
            "CpuQuota": cpu,
            "OomScoreAdj": oom,
        },
        "State": {"Status": status},
        "Image": image,
    }


def ws_labels(ws, container_type):
    return {
        "thoughtmachine.workspace_id": ws,
        "thoughtmachine.container_type": container_type,
    }


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


def _records(ws, vault):
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in storage.iter_record_files(storage.containers_dir(ws, vault))
    ]


def _log_entries(ws, vault):
    path = storage.migrations_log_path(ws, vault)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_synthesises_free_use_and_resource(vault):
    payloads = [
        make_payload("d1", ws_labels(WS, "free_use")),
        make_payload("d2", ws_labels(WS, "resource")),
        make_payload("d3", {"thoughtmachine.container_type": "free_use"}),  # no ws label
    ]
    summary = migration.migrate_records(
        WS, docker_source=FakeDockerClient(payloads), vault_root=vault
    )

    assert summary["scanned"] == 3
    assert summary["created"] == 2
    assert summary["skipped"] == 1
    assert summary["failed"] == 0
    assert summary["aborted"] is False

    records = {r["docker_id"]: r for r in _records(WS, vault)}
    assert set(records) == {"d1", "d2"}
    assert records["d1"]["lifecycle_class"] == "ephemeral"
    assert records["d2"]["lifecycle_class"] == "resource"

    for rec in records.values():
        assert rec["schema_version"] == SCHEMA_VERSION_LEGACY
        assert rec["inferred"] is True
        assert rec["owner"] == "workspace-owned"
        assert rec["state"] == "running"
        assert rec["created_at"] and rec["updated_at"]
        assert len(rec["event_log"]) == 1
        assert rec["event_log"][0]["event_type"] == "synthesised"
        assert rec["event_log"][0]["actor"] == "migration"

    # Partial intent snapshot recovered from inspect (never fabricated).
    snap = records["d1"]["intent_snapshot"]
    assert snap["network_mode"] == "none"
    assert snap["mem_limit"] == "268435456"
    assert snap["image_hash"] == "sha256:abc"
    assert snap["workspace_mode"] == ""

    # Write-ahead log holds one {docker_id, record_id, ts} line per container.
    entries = _log_entries(WS, vault)
    assert len(entries) == 2
    for entry in entries:
        assert set(entry) == {"docker_id", "record_id", "ts"}
        assert entry["record_id"] == records[entry["docker_id"]]["id"]


def test_rerun_is_idempotent(vault):
    payloads = [make_payload("d1", ws_labels(WS, "free_use"))]
    source = FakeDockerClient(payloads)

    first = migration.migrate_records(WS, docker_source=source, vault_root=vault)
    assert first["created"] == 1

    second = migration.migrate_records(WS, docker_source=source, vault_root=vault)
    assert second["created"] == 0
    assert second["skipped"] == 1
    assert len(_records(WS, vault)) == 1
    assert len(_log_entries(WS, vault)) == 1


def test_rematerialises_same_id_when_record_file_missing(vault):
    payloads = [make_payload("d1", ws_labels(WS, "free_use"))]
    source = FakeDockerClient(payloads)

    migration.migrate_records(WS, docker_source=source, vault_root=vault)
    original = _records(WS, vault)[0]
    storage.record_path(WS, original["id"], vault).unlink()
    assert _records(WS, vault) == []

    summary = migration.migrate_records(WS, docker_source=source, vault_root=vault)
    assert summary["rematerialised"] == 1
    assert summary["created"] == 0

    rebuilt = _records(WS, vault)[0]
    assert rebuilt["id"] == original["id"]


def test_unknown_container_type_defaults_to_persistent(vault):
    payloads = [make_payload("d1", ws_labels(WS, "weird"))]
    summary = migration.migrate_records(
        WS, docker_source=FakeDockerClient(payloads), vault_root=vault
    )
    assert summary["created"] == 1

    rec = _records(WS, vault)[0]
    assert rec["lifecycle_class"] == "persistent"
    assert rec["owner"] == "workspace-owned"
    assert "inference" in rec["event_log"][0]["payload"]


def test_daemon_down_aborts_without_writes(vault):
    def boom():
        raise RuntimeError("cannot connect to the docker daemon")

    summary = migration.migrate_records(WS, docker_source=boom, vault_root=vault)
    assert summary["aborted"] is True
    assert summary["created"] == 0
    assert summary["scanned"] == 0
    assert _records(WS, vault) == []


def test_all_workspaces_when_workspace_id_is_none(vault):
    payloads = [
        make_payload("d1", ws_labels("ws-a", "free_use")),
        make_payload("d2", ws_labels("ws-b", "resource")),
        make_payload("d3", {}),  # skipped: no workspace label
    ]
    summary = migration.migrate_records(
        None, docker_source=FakeDockerClient(payloads), vault_root=vault
    )
    assert summary["created"] == 2
    assert summary["skipped"] == 1
    assert len(_records("ws-a", vault)) == 1
    assert len(_records("ws-b", vault)) == 1


def test_iterable_source_is_accepted(vault):
    payloads = [make_payload("d1", ws_labels(WS, "free_use"))]
    summary = migration.migrate_records(WS, docker_source=payloads, vault_root=vault)
    assert summary["created"] == 1


def test_label_with_existing_record_is_skipped(vault):
    """A container already labelled with a record id is never re-synthesised."""
    record_id = "rec-existing"
    api.create_record(
        WS, "persistent", "workspace-owned", id=record_id, vault_root=vault
    )

    payloads = [
        make_payload(
            "d1",
            {**ws_labels(WS, "free_use"), api.RECORD_LABEL_KEY: record_id},
        )
    ]
    summary = migration.migrate_records(
        WS, docker_source=FakeDockerClient(payloads), vault_root=vault
    )

    assert summary["created"] == 0
    assert summary["skipped"] >= 1
    records = _records(WS, vault)
    assert len(records) == 1
    assert records[0]["id"] == record_id


def test_label_missing_record_rematerialises_same_id(vault):
    """The label's record id is ground truth: rebuild the SAME id if lost."""
    record_id = "rec-lost"
    api.create_record(
        WS, "persistent", "workspace-owned", id=record_id, vault_root=vault
    )
    storage.record_path(WS, record_id, vault).unlink()
    assert _records(WS, vault) == []

    payloads = [
        make_payload(
            "d1",
            {**ws_labels(WS, "free_use"), api.RECORD_LABEL_KEY: record_id},
        )
    ]
    summary = migration.migrate_records(
        WS, docker_source=FakeDockerClient(payloads), vault_root=vault
    )

    assert summary["rematerialised"] == 1
    assert summary["created"] == 0
    records = _records(WS, vault)
    assert len(records) == 1
    assert records[0]["id"] == record_id


def test_container_without_label_creates_new_record(vault):
    """With no label and no WAL entry, a fresh migration-authored id is minted."""
    payloads = [make_payload("d1", ws_labels(WS, "free_use"))]
    summary = migration.migrate_records(
        WS, docker_source=FakeDockerClient(payloads), vault_root=vault
    )

    assert summary["created"] == 1
    records = _records(WS, vault)
    assert len(records) == 1
    assert records[0]["id"]
    assert records[0]["id"] != "d1"
