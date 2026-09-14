"""Hermetic tests for the container-RECORD garbage-collector *predicate*.

Companion to ``tests/test_record_lifecycle_gc.py``.  That file pins the
sweeper's lifecycle / liveness / age / retention gates; this file pins the
one-clause **predicate change** layered on top of them: workspace *registration*
is no longer a reap condition.

``sweep_orphan_container_records`` reaps a record whenever its bound container
is gone — whether or not the owning workspace is registered.  The matching case
in the sibling file is
``test_record_lifecycle_gc.py::test_registered_workspace_record_is_also_reaped``.

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
    LIFECYCLE_RESOURCE,
    LIFECYCLE_SERVICE,
    OWNER_WORKSPACE,
    create_record,
    load_record,
)
from thoughtmachine.container_record import storage

# The registered workspace the sweeper must now consider.
_REG_WS = "w-registered"


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
    kwargs.setdefault("registered_workspace_ids", [_REG_WS])
    return sweep_orphan_container_records(**kwargs)


# ---------------------------------------------------------------------------
# (a) the predicate change: a REGISTERED workspace's gone-container record is reaped
# ---------------------------------------------------------------------------


def test_registered_workspace_record_is_reaped():
    # ``docker_id`` names a container that is NOT in the live set (dead id).
    path = _mint(_REG_WS, "rec-1", docker_id="d" * 16, age_days=10)
    result = _sweep(registered_workspace_ids=[_REG_WS])
    assert result["removed"] == 1
    assert result["removed_orphan"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert not path.is_file()
    assert load_record(_REG_WS, "rec-1") is None


# ---------------------------------------------------------------------------
# (b) registered workspace + EMPTY docker_id (no bound container) -> reaped
# ---------------------------------------------------------------------------


def test_registered_workspace_empty_docker_id_is_reaped():
    path = _mint(_REG_WS, "rec-1", docker_id="", age_days=10)
    result = _sweep(registered_workspace_ids=[_REG_WS])
    assert result["removed"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert not path.is_file()


# ---------------------------------------------------------------------------
# (c) the live-container safety clause still protects a REGISTERED workspace
# ---------------------------------------------------------------------------


def test_registered_workspace_live_container_is_retained():
    live = "c" * 16
    path = _mint(_REG_WS, "rec-1", docker_id=live, age_days=10)
    result = _sweep(registered_workspace_ids=[_REG_WS],
                    docker_client=_FakeDocker([live]))
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "container_live" in result["detail"]
    assert path.is_file()


# ---------------------------------------------------------------------------
# (d) own-lifecycle classes stay exempt even in a registered workspace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lifecycle_class", [LIFECYCLE_RESOURCE, LIFECYCLE_SERVICE])
def test_registered_workspace_own_lifecycle_is_exempt(lifecycle_class):
    path = _mint(_REG_WS, "rec-1", lifecycle_class=lifecycle_class, age_days=10)
    result = _sweep(registered_workspace_ids=[_REG_WS])
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "lifecycle_own" in result["detail"]
    assert path.is_file()


# ---------------------------------------------------------------------------
# (e) the age gate still applies to a registered workspace
# ---------------------------------------------------------------------------


def test_registered_workspace_too_young_is_retained():
    path = _mint(_REG_WS, "rec-1", age_days=0)
    result = _sweep(registered_workspace_ids=[_REG_WS])
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "too young" in result["detail"]
    assert path.is_file()


# ---------------------------------------------------------------------------
# (f) per-record retention_days overrides the default window (both directions)
# ---------------------------------------------------------------------------


def test_registered_workspace_retention_days_shrinks_window():
    # 3 days old: young vs a 30-day default, but past its own 1-day retention.
    path = _mint(_REG_WS, "rec-1", age_days=3, retention_days=1)
    result = _sweep(registered_workspace_ids=[_REG_WS],
                    default_max_age_s=30 * 86400)
    assert result["removed"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert not path.is_file()


def test_registered_workspace_retention_days_extends_window():
    # 3 days old: past a 1-day default, but inside its own 30-day retention.
    path = _mint(_REG_WS, "rec-1", age_days=3, retention_days=30)
    result = _sweep(registered_workspace_ids=[_REG_WS],
                    default_max_age_s=86400)
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert path.is_file()


# ---------------------------------------------------------------------------
# (g) dry_run counts a registered-workspace reap but NEVER deletes
# ---------------------------------------------------------------------------


def test_registered_workspace_dry_run_counts_without_deleting():
    path = _mint(_REG_WS, "rec-1", age_days=10)
    result = _sweep(registered_workspace_ids=[_REG_WS], dry_run=True)
    assert result["dry_run"] is True
    assert result["removed"] == 1
    assert result["removed_records"] == ["rec-1"]
    assert path.is_file()
    assert load_record(_REG_WS, "rec-1") is not None


# ---------------------------------------------------------------------------
# (h) an empty/absent registry is still a conservative no-op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("registry", [None, []])
def test_empty_registry_is_still_a_noop(registry):
    path = _mint(_REG_WS, "rec-1", age_days=10)
    result = sweep_orphan_container_records(
        registered_workspace_ids=registry, docker_client=_FakeDocker([]),
    )
    assert result["removed"] == 0
    assert result["removed_records"] == []
    assert result["detail"] == "registry empty; record GC skipped"
    assert path.is_file()


# ---------------------------------------------------------------------------
# (i) a reap is audited (RECORD_REAP) + warned, and emits NO record event
# ---------------------------------------------------------------------------


def test_reap_emits_record_reap_audit_and_warning_not_a_record_event(monkeypatch):
    _mint(_REG_WS, "rec-1", age_days=10)

    audits = []
    logs = []
    events = []
    monkeypatch.setattr(container_manager, "_audit",
                        lambda event, data: audits.append((event, data)))
    monkeypatch.setattr(container_manager, "log",
                        lambda *a, **k: logs.append(a))
    monkeypatch.setattr(container_manager, "log_container_event",
                        lambda *a, **k: events.append(a))

    result = _sweep(registered_workspace_ids=[_REG_WS])

    assert result["removed"] == 1
    assert any(event == "RECORD_REAP" for event, _ in audits)
    assert any(
        "rec-1" in str(data)
        for event, data in audits
        if event == "RECORD_REAP"
    )
    assert any(args and args[0] == "WARNING" for args in logs)
    # The sweeper audits + logs; it must never emit a container *record* event.
    assert events == []


# ---------------------------------------------------------------------------
# (j) docker soft-fail -> nothing removed, no crash (never raises)
# ---------------------------------------------------------------------------


def test_docker_unavailable_soft_fails_for_registered_workspace():
    path = _mint(_REG_WS, "rec-1", age_days=10)

    class _Boom:
        class containers:  # noqa: N801 - attribute namespace
            @staticmethod
            def list(all=True):  # noqa: A002
                raise RuntimeError("daemon down")

    result = sweep_orphan_container_records(
        registered_workspace_ids=[_REG_WS], docker_client=_Boom(),
    )
    assert result["removed"] == 0
    assert result["detail"].startswith("docker unavailable:")
    assert path.is_file()



# ---------------------------------------------------------------------------
# (k) an UNKNOWN lifecycle class fails closed -> never reaped
# ---------------------------------------------------------------------------


def test_registered_workspace_unknown_class_is_skipped():
    # create_record validates the lifecycle class, so mint a KNOWN class and then
    # rewrite the record JSON to an unrecognised one (still dead-bound + old).
    path = _mint(_REG_WS, "rec-1", docker_id="d" * 16, age_days=10)
    data = storage.read_record_file(path)
    data["lifecycle_class"] = "bogus-class"
    storage.write_record_file(path, data)

    result = _sweep(registered_workspace_ids=[_REG_WS])
    assert result["removed"] == 0
    assert result["skipped"] == 1
    # The fail-closed skip is attributed to the unknown class (container_manager
    # line 3401-3402: ``except UnknownLifecycleClass: _note_skip("unknown_class")``).
    assert "unknown_class" in result["detail"]
    assert path.is_file()
    assert load_record(_REG_WS, "rec-1") is not None


# ---------------------------------------------------------------------------
# (l) a reap forgets the record's (workspace, name) entry in the name index
# ---------------------------------------------------------------------------


def test_registered_workspace_reap_forgets_name_index_entry():
    reaped_key = (_REG_WS, "rec-1")
    bystander_key = (_REG_WS, "rec-2")
    container_manager._NAME_INDEX[reaped_key] = "rec-1"
    container_manager._NAME_INDEX[bystander_key] = "rec-2"
    _mint(_REG_WS, "rec-1", docker_id="d" * 16, age_days=10)

    result = _sweep(registered_workspace_ids=[_REG_WS])
    assert result["removed"] == 1
    # container_manager line 3451: ``_name_index_forget(record.id)`` drops every
    # (workspace, name) key whose value is the reaped record id.
    assert reaped_key not in container_manager._NAME_INDEX
    # Only the reaped record's entry is dropped: the bystander survives.
    assert container_manager._NAME_INDEX[bystander_key] == "rec-2"



# ---------------------------------------------------------------------------
# (m) the sweeper emits EXACTLY ONE summary (WARNING + RECORD_SWEEP) per run,
#     on every exit path, and never emits a container *record* event
#     (regression guard for the ``_emit_summary`` boot-visibility change).
# ---------------------------------------------------------------------------


def _collect(monkeypatch):
    """Install list collectors for the sweeper's audit + log side effects."""
    audits, logs, events = [], [], []
    monkeypatch.setattr(container_manager, "_audit",
                        lambda event, data: audits.append((event, data)))
    monkeypatch.setattr(container_manager, "log",
                        lambda *a, **k: logs.append(a))
    monkeypatch.setattr(container_manager, "log_container_event",
                        lambda *a, **k: events.append(a))
    return audits, logs, events


def test_clean_sweep_emits_one_summary_audit_and_warning(monkeypatch):
    audits, logs, events = _collect(monkeypatch)

    result = _sweep(registered_workspace_ids=[_REG_WS],
                    docker_client=_FakeDocker([]))

    assert result["removed"] == 0
    sweeps = [e for e, _ in audits if e == "RECORD_SWEEP"]
    assert len(sweeps) == 1
    warnings = [a for a in logs if a and a[0] == "WARNING"]
    assert len(warnings) == 1
    assert "orphan record sweep" in warnings[0][2]
    assert "reason=clean" in warnings[0][2]
    # The sweeper never emits a container record event.
    assert events == []


def test_docker_unavailable_emits_one_summary(monkeypatch):
    audits, logs, events = _collect(monkeypatch)

    class _Boom:
        class containers:  # noqa: N801 - attribute namespace
            @staticmethod
            def list(all=True):  # noqa: A002
                raise RuntimeError("daemon down")

    result = sweep_orphan_container_records(
        registered_workspace_ids=[_REG_WS], docker_client=_Boom(),
    )

    assert result["detail"].startswith("docker unavailable:")
    # EXACTLY ONE summary audit + EXACTLY ONE summary WARNING, tagged with the
    # exact docker-unavailable detail (not merely its prefix).
    assert len([e for e, _ in audits if e == "RECORD_SWEEP"]) == 1
    warnings = [a for a in logs if a and a[0] == "WARNING"]
    assert len(warnings) == 1
    assert "orphan record sweep" in warnings[0][2]
    assert f"reason={result['detail']}" in warnings[0][2]
    assert events == []


def test_registry_empty_variant_emits_one_summary(monkeypatch):
    audits, logs, _ = _collect(monkeypatch)

    result = sweep_orphan_container_records(
        registered_workspace_ids=[], docker_client=_FakeDocker([]),
    )

    assert result["detail"] == "registry empty; record GC skipped"
    assert len([e for e, _ in audits if e == "RECORD_SWEEP"]) == 1
    warnings = [a for a in logs if a and a[0] == "WARNING"]
    assert len(warnings) == 1
    assert "reason=registry empty; record GC skipped" in warnings[0][2]


def test_real_reap_still_emits_record_reap_and_one_summary(monkeypatch):
    _mint(_REG_WS, "rec-1", docker_id="d" * 16, age_days=10)
    audits, logs, events = _collect(monkeypatch)

    result = _sweep(registered_workspace_ids=[_REG_WS])

    assert result["removed"] == 1
    assert any(e == "RECORD_REAP" for e, _ in audits)
    reaped_warnings = [
        a for a in logs
        if a and a[0] == "WARNING" and "reaped orphan container record" in str(a)
    ]
    assert len(reaped_warnings) == 1
    assert len([e for e, _ in audits if e == "RECORD_SWEEP"]) == 1
    assert events == []


# ---------------------------------------------------------------------------
# (n) the summary fires EXACTLY ONCE on EVERY return path of the sweeper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected_detail",
    [
        ("registry_empty", "registry empty; record GC skipped"),
        ("no_sdk", "docker SDK not installed"),
        ("from_env_fail", "docker unavailable: boom"),
        ("list_fail", "docker unavailable: daemon down"),
        ("clean", ""),
    ],
)
def test_summary_emitted_exactly_once_on_every_return_path(
        path, expected_detail, monkeypatch):
    audits, logs, events = _collect(monkeypatch)

    # No records are minted: every path below returns before (or without) a
    # reap, so the *only* emitted WARNING must be the one summary line.
    kwargs = {"registered_workspace_ids": [_REG_WS]}

    if path == "registry_empty":
        kwargs["registered_workspace_ids"] = []
        kwargs["docker_client"] = _FakeDocker([])
    elif path == "no_sdk":
        monkeypatch.setattr(container_manager, "DOCKER_AVAILABLE", False)
        kwargs["docker_client"] = None
    elif path == "from_env_fail":
        monkeypatch.setattr(container_manager, "DOCKER_AVAILABLE", True)

        class _NoDaemon:
            @staticmethod
            def from_env():
                raise RuntimeError("boom")

        monkeypatch.setattr(container_manager, "docker", _NoDaemon())
        kwargs["docker_client"] = None
    elif path == "list_fail":
        class _Boom:
            class containers:  # noqa: N801 - attribute namespace
                @staticmethod
                def list(all=True):  # noqa: A002
                    raise RuntimeError("daemon down")

        kwargs["docker_client"] = _Boom()
    elif path == "clean":
        kwargs["docker_client"] = _FakeDocker([])

    result = sweep_orphan_container_records(**kwargs)

    assert result["detail"] == expected_detail
    # EXACTLY ONE summary audit + EXACTLY ONE summary WARNING ...
    assert len([e for e, _ in audits if e == "RECORD_SWEEP"]) == 1
    warnings = [a for a in logs if a and a[0] == "WARNING"]
    assert len(warnings) == 1
    assert "orphan record sweep" in warnings[0][2]
    assert f"reason={result['detail'] or 'clean'}" in warnings[0][2]
    # ... and never a container *record* event.
    assert events == []

