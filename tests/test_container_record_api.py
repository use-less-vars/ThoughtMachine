"""Tests for the container-record API (design §3 / §7)."""

from __future__ import annotations

import json
import time

import pytest

from thoughtmachine.container_record import api, storage
from thoughtmachine.container_record.models import (
    SCHEMA_VERSION_CURRENT,
    STATE_CREATING,
    RecordCorrupt,
    RecordNotFound,
)

WS = "ws-1"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(root))
    return root


def test_create_record_defaults(vault):
    rec = api.create_record(
        WS, "ephemeral", "workspace-owned", purpose="p", vault_root=vault
    )
    assert rec.id
    assert rec.lifecycle_class == "ephemeral"
    assert rec.owner == "workspace-owned"
    assert rec.purpose == "p"
    assert rec.docker_id == ""
    # create_record uses the §1 field default for ``state`` (""), not the
    # pre-run proposal — that lives in begin_record.
    assert rec.state == ""
    assert rec.schema_version == SCHEMA_VERSION_CURRENT
    assert rec.inferred is False
    assert rec.event_log == []
    assert rec.created_at and rec.updated_at

    reloaded = api.load_record(WS, rec.id, vault)
    assert reloaded.to_dict() == rec.to_dict()


def test_create_record_validates_enums(vault):
    with pytest.raises(ValueError):
        api.create_record(WS, "bogus", "workspace-owned", vault_root=vault)
    with pytest.raises(ValueError):
        api.create_record(WS, "ephemeral", "nobody", vault_root=vault)


def test_create_record_with_id_is_idempotent(vault):
    first = api.create_record(
        WS, "ephemeral", "workspace-owned", purpose="first", id="fixed", vault_root=vault
    )
    second = api.create_record(
        WS, "persistent", "system-owned", purpose="second", id="fixed", vault_root=vault
    )
    assert first.id == second.id == "fixed"
    assert second.purpose == "first"  # unchanged
    assert second.lifecycle_class == "ephemeral"


def test_load_missing_returns_none(vault):
    assert api.load_record(WS, "nope", vault) is None


def test_mutators_missing_raise_record_not_found(vault):
    with pytest.raises(RecordNotFound):
        api.update_record(WS, "nope", vault_root=vault, purpose="x")
    with pytest.raises(RecordNotFound):
        api.append_event(WS, "nope", "evt", "actor", vault_root=vault)
    with pytest.raises(RecordNotFound):
        api.delete_record(WS, "nope", vault)


def test_update_bumps_updated_at_and_persists(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    time.sleep(0.01)
    updated = api.update_record(
        WS, rec.id, vault_root=vault, purpose="new", state="running"
    )
    assert updated.purpose == "new"
    assert updated.state == "running"
    assert updated.updated_at != rec.updated_at
    assert api.load_record(WS, rec.id, vault).purpose == "new"


def test_update_rejects_unknown_and_immutable_fields(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    with pytest.raises(ValueError):
        api.update_record(WS, rec.id, vault_root=vault, nope="x")
    # `id` is a bound positional parameter of update_record, so it can never
    # reach the change-set; supplying it collides at the call boundary.
    with pytest.raises(TypeError):
        api.update_record(WS, rec.id, vault_root=vault, id="other")
    with pytest.raises(ValueError):
        api.update_record(WS, rec.id, vault_root=vault, created_at="t")


def test_created_and_updated_at_are_never_caller_settable(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    original_created = rec.created_at
    # created_at is immutable; updated_at is overwritten on every mutation.
    with pytest.raises(ValueError):
        api.update_record(WS, rec.id, vault_root=vault, created_at="1999-01-01")
    bumped = api.update_record(
        WS, rec.id, vault_root=vault, updated_at="1999-01-01", purpose="x"
    )
    assert bumped.created_at == original_created
    assert bumped.updated_at != "1999-01-01"


def test_delete_record_removes_file_and_sidecar(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    sidecar = storage.events_path(WS, rec.id, vault)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("", encoding="utf-8")

    api.delete_record(WS, rec.id, vault)
    assert api.load_record(WS, rec.id, vault) is None
    assert not sidecar.exists()


def test_list_records_quarantines_and_raises_on_corrupt(vault):
    good = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    bad = storage.record_path(WS, "bad", vault)
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("garbage", encoding="utf-8")

    with pytest.raises(RecordCorrupt):
        api.list_records(WS, vault)

    # Quarantined (self-healing): the corrupt file is renamed away, so an
    # immediate retry now succeeds.
    assert not bad.exists()
    leftovers = [p.name for p in bad.parent.iterdir()]
    assert any(name.startswith("bad.json.corrupt-") for name in leftovers)

    assert [r.id for r in api.list_records(WS, vault)] == [good.id]


def test_find_by_docker_label_resolves_by_record_id_only(vault):
    api.create_record(
        WS, "ephemeral", "workspace-owned", id="rid", vault_root=vault
    )
    api.update_record(WS, "rid", vault_root=vault, docker_id="dock1")

    # The label value is the record's ``id`` (§7, :344/:347).
    assert api.find_by_docker_label("rid", vault).id == "rid"
    # No docker_id-field fallback is offered (§7 does not sanction one).
    assert api.find_by_docker_label("dock1", vault) is None
    assert api.find_by_docker_label("absent", vault) is None


def test_record_label_shape(vault):
    assert api.RECORD_LABEL_KEY == "thoughtmachine.container_id"
    assert api.record_label("abc") == {api.RECORD_LABEL_KEY: "abc"}


def test_two_phase_begin_and_attach(vault):
    rec = api.begin_record(WS, "resource", "workspace-owned", vault_root=vault)
    assert rec.state == STATE_CREATING
    assert rec.docker_id == ""

    time.sleep(0.01)
    attached = api.attach_container(
        WS, rec.id, "docker-xyz", state="running", vault_root=vault
    )
    assert attached.docker_id == "docker-xyz"
    assert attached.state == "running"
    assert attached.updated_at != rec.updated_at

    no_state = api.attach_container(WS, rec.id, "docker-xyz", vault_root=vault)
    assert no_state.state == "running"  # left unchanged


def test_append_event_entry_shape(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    time.sleep(0.01)
    updated = api.append_event(
        WS, rec.id, "started", "tester", vault_root=vault, foo="bar"
    )
    assert updated.updated_at != rec.updated_at

    entries = api.load_record(WS, rec.id, vault).event_log
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry) == {"timestamp", "event_type", "actor", "payload"}
    assert entry["event_type"] == "started"
    assert entry["actor"] == "tester"
    assert entry["payload"] == {"foo": "bar"}


def test_load_corrupt_raises_record_corrupt(vault):
    path = storage.record_path(WS, "bad", vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(RecordCorrupt):
        api.load_record(WS, "bad", vault)


def test_read_event_log_merges_embedded(vault):
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    api.append_event(WS, rec.id, "started", "tester", vault_root=vault, foo="bar")
    log = api.read_event_log(WS, rec.id, vault_root=vault)
    assert len(log) == 1 and log[0]["event_type"] == "started"
    # Missing record is a benign miss.
    assert api.read_event_log(WS, "absent", vault_root=vault) == []


def test_event_log_moves_to_sidecar_above_ceiling(vault, monkeypatch):
    monkeypatch.setattr(storage, "EVENT_LOG_CEILING_BYTES", 10)
    rec = api.create_record(WS, "ephemeral", "workspace-owned", vault_root=vault)
    sidecar = storage.events_path(WS, rec.id, vault)
    assert not sidecar.is_file()

    for n in range(5):
        api.append_event(WS, rec.id, "tick", "actor", vault_root=vault, n=n)
        if sidecar.is_file():
            break

    assert sidecar.is_file()
    # The embedded event_log is now a pointer, not empty.
    embedded = api.load_record(WS, rec.id, vault).event_log
    assert len(embedded) == 1
    pointer = embedded[0]
    assert pointer["type"] == "event_log_pointer"
    assert pointer["sidecar"] == f"{rec.id}.events.jsonl"
    assert pointer["count"] == 1

    # A further append goes to the sidecar, never back into the record.
    api.append_event(WS, rec.id, "tick", "actor", vault_root=vault, n=99)
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
    assert json.loads(lines[-1])["payload"] == {"n": 99}
    embedded = api.load_record(WS, rec.id, vault).event_log
    assert len(embedded) == 1 and embedded[0]["type"] == "event_log_pointer"
    assert embedded[0]["count"] == len([line for line in lines if line.strip()])

    # read_event_log merges the sidecar (first) with the embedded entries.
    merged = api.read_event_log(WS, rec.id, vault_root=vault)
    assert [entry["payload"].get("n") for entry in merged][-1] == 99
