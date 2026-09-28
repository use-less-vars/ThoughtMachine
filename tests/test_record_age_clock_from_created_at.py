"""Regression tests: the orphan RECORD GC must age records by ``created_at``.

Background (bug/drift-scan-resets-record-age-clock)
---------------------------------------------------
``sweep_orphan_container_records`` decided a record's age from ``updated_at``
*first* (falling back to ``created_at``).  But ``updated_at`` is REWRITTEN by
any non-lifecycle write to the record -- notably the startup drift scan
(``emit_drift_findings`` -> ``append_event``), which bumps ``updated_at``
without touching ``created_at`` (which is immutable, see
``api._IMMUTABLE_FIELDS``).

Consequence: a genuinely ancient orphan record that the drift scan happens to
touch has its age clock silently reset to "just now", so
``now - updated_at < max_age_s`` reports it as "too young" and the sweeper
never reaps it.  This defeats the age gate entirely for records the drift
scan visits.

The fix ages records from the IMMUTABLE ``created_at`` (falling back to
``updated_at`` only when ``created_at`` is absent).  ``created_at`` is set
exactly once, at record creation, and can never be bumped -- so the age clock
can no longer be reset by a drift write.

The Docker client is a fake (never a live daemon); the vault is a tmp dir via
``THOUGHTMACHINE_VAULT_ROOT`` so records are minted into an isolated store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import infra.container_manager as container_manager
from infra.container_manager import sweep_orphan_container_records
from thoughtmachine.container_record import (
    LIFECYCLE_PERSISTENT,
    OWNER_WORKSPACE,
    create_record,
    load_record,
)
from thoughtmachine.container_record import storage

_WS = "w-orphan"


# ---------------------------------------------------------------------------
# Fakes (mirror tests/test_record_lifecycle_gc.py)
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


def _mint(workspace_id, record_id, *, docker_id="", lifecycle_class=LIFECYCLE_PERSISTENT,
          created_age_days=None, updated_age_days=None):
    """Mint a record, then rewrite its JSON with INDEPENDENT created/updated ages.

    Unlike the helper in ``tests/test_record_lifecycle_gc.py`` (which ties
    ``created_at`` and ``updated_at`` to the SAME instant), this lets a test
    model a drift-scan touch: an OLD ``created_at`` with a FRESH ``updated_at``.
    """
    create_record(
        workspace_id, lifecycle_class, OWNER_WORKSPACE,
        id=record_id, name=record_id,
    )
    path = storage.record_path(workspace_id, record_id)
    data = storage.read_record_file(path)
    data["docker_id"] = docker_id
    now = datetime.now(timezone.utc)
    if created_age_days is not None:
        data["created_at"] = (now - timedelta(days=created_age_days)).isoformat()
    if updated_age_days is not None:
        data["updated_at"] = (now - timedelta(days=updated_age_days)).isoformat()
    storage.write_record_file(path, data)
    return path


def _sweep(**kwargs):
    kwargs.setdefault("docker_client", _FakeDocker([]))
    kwargs.setdefault("registered_workspace_ids", [_WS])
    return sweep_orphan_container_records(**kwargs)


# ---------------------------------------------------------------------------
# Test A -- the reported bug: a drift-touched old orphan must still be reaped.
#   created_at = 10 days old, updated_at = now (the drift scan bumped it).
#   RED on the buggy tree (aged from updated_at -> "too young"); GREEN after.
# ---------------------------------------------------------------------------


def test_old_orphan_with_fresh_updated_at_is_reaped():
    path = _mint(_WS, "rec-1", docker_id="c" * 16,
                 created_age_days=10, updated_age_days=0)
    result = _sweep()
    assert result["removed"] == 1
    assert result["removed_orphan"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert result["skipped"] == 0
    assert not path.is_file()
    assert load_record(_WS, "rec-1") is None


# ---------------------------------------------------------------------------
# Test B -- the live-set guard still holds (unaffected by the fix).
#   A record bound to a LIVE container is retained no matter how old.
#   PASSES before and after (regression guard).
# ---------------------------------------------------------------------------


def test_live_container_is_still_skipped():
    live = "c" * 16
    path = _mint(_WS, "rec-1", docker_id=live, created_age_days=10,
                 updated_age_days=10)
    result = _sweep(docker_client=_FakeDocker([live]))
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "container_live" in result["detail"]
    assert path.is_file()


# ---------------------------------------------------------------------------
# Test C -- the just-written / mid-create record is still protected.
#   created_at == updated_at == now: an unbindable fresh record stays "too
#   young" and is retained.  This is the invariant the created_at basis
#   PRESERVES (created_at is immutable and written at create time).
#   PASSES before and after (regression guard).
# ---------------------------------------------------------------------------


def test_freshly_written_orphan_is_retained():
    path = _mint(_WS, "rec-1", docker_id="", created_age_days=0,
                 updated_age_days=0)
    result = _sweep()
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "too young" in result["detail"]
    assert path.is_file()
