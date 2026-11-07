"""Invariant guard: ``Record.updated_at`` advances on MUTATION only.

``updated_at`` is the record's last-write clock.  The invariant this file pins
is two-sided:

* **Mutation bumps it.**  Every sanctioned writer -- ``api.update_record`` and
  ``api.append_event`` -- rewrites ``updated_at`` to a fresh ISO-8601 instant
  strictly newer than the value it replaced.  This is a REAL clock bump (the
  record is minted with a back-dated ``updated_at`` and the writer must move it
  forward), never a truthy placeholder.
* **Inspection does NOT bump it.**  Reading the record -- a ``to_dict`` ->
  ``from_dict`` serialization round-trip, or the orphan-record sweeper's
  age-read (``sweep_orphan_container_records``) -- leaves ``updated_at`` exactly
  as it was.  A read is not a write.

Background
----------
If inspection bumped ``updated_at`` the field would stop being a mutation
clock: a pure read (the startup drift scan, a display render, the sweeper's age
gate) would silently make a stale record look "just touched".  The mutation-only
contract is documented on the field itself in
``thoughtmachine.container_record.models.Record``.

Hermetic
--------
No Docker daemon (the docker client is a fake), no network.  The record store
is a tmp dir via ``THOUGHTMACHINE_VAULT_ROOT``.  Record construction + store
fixture mirror ``tests/test_record_age_clock_from_created_at.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import infra.container_manager as container_manager
from infra.container_manager import sweep_orphan_container_records
from thoughtmachine.container_record import (
    LIFECYCLE_PERSISTENT,
    OWNER_WORKSPACE,
    Record,
    append_event,
    create_record,
    load_record,
    update_record,
)
from thoughtmachine.container_record import storage

_WS = "w-updated-at"


# ---------------------------------------------------------------------------
# Fakes (mirror tests/test_record_age_clock_from_created_at.py)
# ---------------------------------------------------------------------------


class _FakeContainer:
    def __init__(self, container_id):
        self.id = container_id


class _FakeContainers:
    def __init__(self, ids):
        self._ids = list(ids)

    def list(self, all=True):  # noqa: A002 - mirrors the Docker SDK signature
        return [_FakeContainer(i) for i in self._ids]


class _FakeDocker:
    def __init__(self, live_ids=()):
        self.containers = _FakeContainers(live_ids)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    """Isolated default vault + a cleared module-level ``_NAME_INDEX``."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

    def _reset():
        container_manager._NAME_INDEX.clear()
        container_manager._NAME_INDEX_COLLISIONS.clear()
        container_manager._NAME_INDEX_BUILT.clear()

    _reset()
    yield
    _reset()


def _parse_iso(value):
    """Parse *value* as a tz-aware ISO-8601 instant (fails the test if not)."""
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f"not a tz-aware ISO-8601 instant: {value!r}"
    return parsed


def _mint_backdated(workspace_id, record_id, *, docker_id="", age_seconds=3600):
    """Mint a record, then back-date BOTH clocks to *age_seconds* in the past.

    ``create_record`` sets ``created_at == updated_at == now``; rewriting the
    file independent of the API models an untouched record whose write clock is
    genuinely old, so a subsequent MUTATION bump is strictly observable (a real
    clock bump, never a truthy placeholder).  Returns the parsed ``Record``.
    """
    create_record(
        workspace_id,
        LIFECYCLE_PERSISTENT,
        OWNER_WORKSPACE,
        id=record_id,
        name=record_id,
    )
    path = storage.record_path(workspace_id, record_id)
    data = storage.read_record_file(path)
    data["docker_id"] = docker_id
    past = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    data["created_at"] = past
    data["updated_at"] = past
    storage.write_record_file(path, data)
    return load_record(workspace_id, record_id)


def _mint_with_docker_id(workspace_id, record_id, *, docker_id, age_seconds):
    """Mint a record bound to *docker_id* with both clocks back-dated."""
    return _mint_backdated(
        workspace_id, record_id, docker_id=docker_id, age_seconds=age_seconds
    )


# ---------------------------------------------------------------------------
# (1) MUTATION BUMPS -- two writers, two assertions.
# ---------------------------------------------------------------------------


def test_update_record_advances_updated_at():
    """``update_record`` advances ``updated_at`` to a strictly newer instant."""
    rec = _mint_backdated(_WS, "rec-update")
    before = rec.updated_at

    updated = update_record(_WS, "rec-update", purpose="touched")
    after = updated.updated_at

    assert after != before
    assert _parse_iso(after) > _parse_iso(before)
    # The bumped clock is the *persisted* one, not just the in-memory copy.
    assert load_record(_WS, "rec-update").updated_at == after


def test_append_event_advances_updated_at():
    """``append_event`` advances ``updated_at`` to a strictly newer instant."""
    rec = _mint_backdated(_WS, "rec-event")
    before = rec.updated_at

    appended = append_event(_WS, "rec-event", "note.added", "tester", note="hi")
    after = appended.updated_at

    assert after != before
    assert _parse_iso(after) > _parse_iso(before)
    assert load_record(_WS, "rec-event").updated_at == after


# ---------------------------------------------------------------------------
# (2) INSPECTION DOES NOT BUMP -- two read paths, two assertions.
# ---------------------------------------------------------------------------


def test_serialization_round_trip_does_not_advance_updated_at():
    """A ``to_dict`` -> ``from_dict`` round-trip is a read: it never bumps."""
    rec = _mint_backdated(_WS, "rec-roundtrip")
    before = rec.updated_at

    round_tripped = Record.from_dict(rec.to_dict())

    assert round_tripped.updated_at == before


def test_sweep_age_read_does_not_advance_updated_at():
    """The sweeper's AGE-READ never bumps ``updated_at``.

    A gone-container record that is still "too young" is READ (its age gate
    evaluates ``created_at``/``updated_at``) but RETAINED; its write clock must
    be exactly what it was before the sweep.
    """
    rec = _mint_with_docker_id(
        _WS, "rec-sweep", docker_id="c" * 16, age_seconds=0
    )
    before = rec.updated_at

    result = sweep_orphan_container_records(
        docker_client=_FakeDocker([]), registered_workspace_ids=[_WS]
    )

    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "too young" in result["detail"]
    assert load_record(_WS, "rec-sweep").updated_at == before
