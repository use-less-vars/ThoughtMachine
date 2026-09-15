"""Hermetic tests for the orphan container-RECORD garbage collector.

Exercises ``infra.container_manager.sweep_orphan_container_records`` — the
module-level sweeper that reaps records whose bound container is gone,
honouring lifecycle policy (``own_lifecycle``), liveness (``docker_id`` in the
live set), age (``updated_at`` / ``created_at``) and the per-record
``retention_days`` window.

The Docker client is a fake (never a live daemon); the vault is a tmp dir via
``THOUGHTMACHINE_VAULT_ROOT`` so records are minted into an isolated store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import infra.container_manager as container_manager
from infra.container_manager import sweep_orphan_container_records
from thoughtmachine.container_record import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PERSISTENT,
    LIFECYCLE_RESOURCE,
    LIFECYCLE_SERVICE,
    OWNER_WORKSPACE,
    create_record,
    load_record,
)
from thoughtmachine.container_record import storage

_ORPHAN_WS = "w-orphan"
_KEPT_WS = "w-kept"


# ---------------------------------------------------------------------------
# Fakes
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


def _mint(workspace_id, record_id, *, lifecycle_class=LIFECYCLE_PERSISTENT,
          docker_id="", age_days=None, retention_days=None, name=None):
    """Mint a record, then rewrite its JSON to force the GC-relevant fields."""
    create_record(
        workspace_id, lifecycle_class, OWNER_WORKSPACE,
        id=record_id, name=name if name is not None else record_id,
    )
    path = storage.record_path(workspace_id, record_id)
    data = storage.read_record_file(path)
    data["docker_id"] = docker_id
    if age_days is not None:
        ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        data["created_at"] = ts
        data["updated_at"] = ts
    if retention_days is not None:
        data["retention_days"] = retention_days
    storage.write_record_file(path, data)
    return path


def _sweep(**kwargs):
    kwargs.setdefault("docker_client", _FakeDocker([]))
    kwargs.setdefault("registered_workspace_ids", [_KEPT_WS])
    return sweep_orphan_container_records(**kwargs)


# ---------------------------------------------------------------------------
# (a) conservative no-op on an empty/absent registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("registry", [None, []])
def test_empty_registry_is_a_noop(registry):
    _mint(_ORPHAN_WS, "rec-1", age_days=10)
    result = sweep_orphan_container_records(
        registered_workspace_ids=registry, docker_client=_FakeDocker([]),
    )
    assert result["removed"] == 0
    assert result["removed_records"] == []
    assert result["detail"] == "registry empty; record GC skipped"
    assert load_record(_ORPHAN_WS, "rec-1") is not None


# ---------------------------------------------------------------------------
# (b) orphan + gone container + past retention -> reaped from disk
# ---------------------------------------------------------------------------


def test_orphan_old_record_is_reaped():
    path = _mint(_ORPHAN_WS, "rec-1", docker_id="c" * 16, age_days=10)
    result = _sweep()
    assert result["removed"] == 1
    assert result["removed_orphan"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert result["skipped"] == 0
    assert not path.is_file()
    assert load_record(_ORPHAN_WS, "rec-1") is None


# ---------------------------------------------------------------------------
# (c) registered workspace -> its gone-container record is STILL reaped
# ---------------------------------------------------------------------------


def test_registered_workspace_record_is_also_reaped():
    # Workspace registration is not a reap condition: a record whose bound
    # container is gone is reaped from a registered workspace too.
    path = _mint(_KEPT_WS, "rec-1", age_days=10)
    result = _sweep(registered_workspace_ids=[_KEPT_WS])
    assert result["removed"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert not path.is_file()


# ---------------------------------------------------------------------------
# (d) own-lifecycle classes are exempt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lifecycle_class", [LIFECYCLE_RESOURCE, LIFECYCLE_SERVICE])
def test_own_lifecycle_classes_are_exempt(lifecycle_class):
    path = _mint(_ORPHAN_WS, "rec-1", lifecycle_class=lifecycle_class,
                 age_days=10)
    result = _sweep()
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "lifecycle_own" in result["detail"]
    assert path.is_file()


# ---------------------------------------------------------------------------
# (e) a record bound to a LIVE container is never reaped
# ---------------------------------------------------------------------------


def test_live_container_is_skipped():
    live = "c" * 16
    path = _mint(_ORPHAN_WS, "rec-1", docker_id=live, age_days=10)
    result = _sweep(docker_client=_FakeDocker([live]))
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "container_live" in result["detail"]
    assert path.is_file()


# ---------------------------------------------------------------------------
# (f) too-young records are retained
# ---------------------------------------------------------------------------


def test_too_young_record_is_skipped():
    path = _mint(_ORPHAN_WS, "rec-1", age_days=0)
    result = _sweep()
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "too young" in result["detail"]
    assert path.is_file()


# ---------------------------------------------------------------------------
# (g) retention_days overrides the default window (both directions)
# ---------------------------------------------------------------------------


def test_retention_days_shrinks_window_reaps_young_record():
    # 3 days old: young vs a 30-day default, but past its own 1-day retention.
    path = _mint(_ORPHAN_WS, "rec-1", age_days=3, retention_days=1)
    result = _sweep(default_max_age_s=30 * 86400)
    assert result["removed"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert not path.is_file()


def test_retention_days_extends_window_retains_record():
    # 3 days old: past a 1-day default, but inside its own 30-day retention.
    path = _mint(_ORPHAN_WS, "rec-1", age_days=3, retention_days=30)
    result = _sweep(default_max_age_s=86400)
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert path.is_file()


# ---------------------------------------------------------------------------
# (h) dry_run counts would-be removals but NEVER deletes
# ---------------------------------------------------------------------------


def test_dry_run_counts_without_deleting():
    path = _mint(_ORPHAN_WS, "rec-1", age_days=10)
    result = _sweep(dry_run=True)
    assert result["dry_run"] is True
    assert result["removed"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert path.is_file()
    assert load_record(_ORPHAN_WS, "rec-1") is not None


# ---------------------------------------------------------------------------
# (i) reaping keeps the in-memory name index consistent
# ---------------------------------------------------------------------------


def test_reap_forgets_name_index_entry():
    key = (_ORPHAN_WS, "rec-1")
    container_manager._NAME_INDEX[key] = "rec-1"
    _mint(_ORPHAN_WS, "rec-1", age_days=10)
    result = _sweep()
    assert result["removed"] == 1
    assert key not in container_manager._NAME_INDEX


# ---------------------------------------------------------------------------
# (j) docker soft-fail -> nothing removed, no crash
# ---------------------------------------------------------------------------


def test_docker_unavailable_soft_fails():
    path = _mint(_ORPHAN_WS, "rec-1", age_days=10)

    class _Boom:
        class containers:  # noqa: N801 - attribute namespace
            @staticmethod
            def list(all=True):  # noqa: A002
                raise RuntimeError("daemon down")

    result = sweep_orphan_container_records(
        registered_workspace_ids=[_KEPT_WS], docker_client=_Boom(),
    )
    assert result["removed"] == 0
    assert result["detail"].startswith("docker unavailable:")
    assert path.is_file()


# ---------------------------------------------------------------------------
# (k) per-CLASS policy TTL governs the age gate (ephemeral is bounded, 24 h)
#     R3 precedence: per-record retention_days -> per-class
#     policy.workspace_gc_max_age_s -> global default_max_age_s.
#     For an ephemeral record the effective TTL is the POLICY value (86400 s),
#     NOT the run's global default.
# ---------------------------------------------------------------------------


def test_ephemeral_ttl_comes_from_policy_not_global_default():
    # Two days old: past the 1-day (86400 s) ephemeral policy TTL, but well
    # inside a 30-day global default.  The policy value wins -> reaped.
    path = _mint(_ORPHAN_WS, "rec-1", lifecycle_class=LIFECYCLE_EPHEMERAL,
                 age_days=2)
    result = _sweep(default_max_age_s=30 * 86400)
    assert result["removed"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert not path.is_file()


def test_ephemeral_young_record_retained_despite_short_global_default():
    # Two hours old: younger than the 1-day ephemeral policy TTL, but older
    # than a 1-hour global default.  The policy value wins -> still "too young".
    path = _mint(_ORPHAN_WS, "rec-1", lifecycle_class=LIFECYCLE_EPHEMERAL,
                 age_days=2 / 24)
    result = _sweep(default_max_age_s=3600)
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "too young" in result["detail"]
    assert path.is_file()


# ---------------------------------------------------------------------------
# (l) per-CLASS policy TTL governs PERSISTENT too (86400 s), regardless of the
#     run's global default — parity with the ephemeral pair above.
# ---------------------------------------------------------------------------


def test_persistent_ttl_comes_from_policy_not_global_default():
    # Two days old: past the 1-day (86400 s) persistent policy TTL, but well
    # inside a 30-day global default.  The policy value wins -> reaped.
    path = _mint(_ORPHAN_WS, "rec-1", lifecycle_class=LIFECYCLE_PERSISTENT,
                 age_days=2)
    result = _sweep(default_max_age_s=30 * 86400)
    assert result["removed"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert not path.is_file()

