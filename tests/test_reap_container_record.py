"""Hermetic tests for the ``reap_container_record`` force-reap primitive.

Companion to ``tests/test_record_gc_predicate.py``.  That file pins the
age-gated ``sweep_orphan_container_records``; this one pins the *manual* escape
hatch layered on top: ``infra.container_manager.reap_container_record`` reaps a
single record by id **bypassing the AGE gate only** -- every other safety gate
(own-lifecycle, live container, missing docker daemon) stays in force.

The Docker client is a fake (never a live daemon); the vault is a tmp dir via
``THOUGHTMACHINE_VAULT_ROOT`` so records are minted into an isolated store.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import infra.container_manager as container_manager
from infra.container_manager import ContainerManager, reap_container_record
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

# The workspace these tests mint records into.
_WS = "w-verify"


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
    """A docker-shaped fake usable both as a passed-in client and as the
    module-level ``docker`` (its ``from_env`` returns itself)."""

    def __init__(self, live_ids=()):
        self.containers = _FakeContainers(live_ids)

    def from_env(self):
        return self


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    """Isolated default vault + a cleared module-level name index."""
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


# ---------------------------------------------------------------------------
# (4) apply=True force-reaps: file gone, name index forgotten, bystander intact
# ---------------------------------------------------------------------------


def test_apply_force_reaps_record_and_forgets_name_index():
    reaped_key = (_WS, "rec-1")
    bystander_key = (_WS, "rec-2")
    container_manager._NAME_INDEX[reaped_key] = "rec-1"
    container_manager._NAME_INDEX[bystander_key] = "rec-2"
    path = _mint(_WS, "rec-1", docker_id="d" * 16, age_days=10)

    result = reap_container_record(
        _WS, "rec-1", apply=True, docker_client=_FakeDocker([]))

    assert result["found"] is True
    assert result["reaped"] is True
    assert result["applied"] is True
    assert result["reason"] == "ok"
    assert not path.is_file()
    assert load_record(_WS, "rec-1") is None
    # Only the reaped record's index entry is dropped.
    assert reaped_key not in container_manager._NAME_INDEX
    assert container_manager._NAME_INDEX[bystander_key] == "rec-2"


# ---------------------------------------------------------------------------
# (5) apply=False dry run: nothing written, nothing audited
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_emits_no_force_audit(monkeypatch):
    path = _mint(_WS, "rec-1", docker_id="d" * 16, age_days=10)
    audits = []
    monkeypatch.setattr(container_manager, "_audit",
                        lambda event, data: audits.append((event, data)))

    result = reap_container_record(
        _WS, "rec-1", apply=False, docker_client=_FakeDocker([]))

    assert result["applied"] is False
    assert result["reaped"] is True
    assert result["reason"] == "dry_run"
    assert path.is_file()
    assert load_record(_WS, "rec-1") is not None
    assert not any(event == "RECORD_FORCE_REAP" for event, _ in audits)
    assert not any(event == "RECORD_REAP" for event, _ in audits)


# ---------------------------------------------------------------------------
# (6) a LIVE container is refused even with apply=True
# ---------------------------------------------------------------------------


def test_live_container_is_refused_even_with_apply(monkeypatch):
    live = "c" * 16
    path = _mint(_WS, "rec-1", docker_id=live, age_days=10)
    audits = []
    monkeypatch.setattr(container_manager, "_audit",
                        lambda event, data: audits.append((event, data)))

    result = reap_container_record(
        _WS, "rec-1", apply=True, docker_client=_FakeDocker([live]))

    assert result["reaped"] is False
    assert result["applied"] is False
    assert result["reason"] == "container_live"
    assert path.is_file()
    assert load_record(_WS, "rec-1") is not None
    # The live-container refusal short-circuits BEFORE any audit is emitted:
    # no RECORD_REAP / RECORD_FORCE_REAP *and* no other entry at all.
    assert audits == []


# ---------------------------------------------------------------------------
# (7) the AGE gate is bypassed; the LIVENESS gate never is
# ---------------------------------------------------------------------------


def test_young_record_is_force_reaped_bypassing_age_gate():
    # age_days=0 -> far younger than any retention window; the sweeper would
    # skip it, but the escape hatch reaps it.
    path = _mint(_WS, "rec-1", docker_id="d" * 16, age_days=0)
    result = reap_container_record(
        _WS, "rec-1", apply=True, docker_client=_FakeDocker([]))
    assert result["reaped"] is True
    assert result["reason"] == "ok"
    assert not path.is_file()


def test_young_record_with_live_container_is_still_refused():
    live = "c" * 16
    path = _mint(_WS, "rec-1", docker_id=live, age_days=0)
    result = reap_container_record(
        _WS, "rec-1", apply=True, docker_client=_FakeDocker([live]))
    assert result["reaped"] is False
    assert result["reason"] == "container_live"
    assert path.is_file()


# ---------------------------------------------------------------------------
# (8) the name index is built lazily (cold until _ensure_name_index)
# ---------------------------------------------------------------------------


def test_name_index_is_cold_until_built():
    _mint(_WS, "rec-9", name="my-box")

    # __new__ bypasses __init__ (no docker client needed).
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_id = _WS

    assert cm._record_for_name("my-box") is None  # cold: index not built yet
    cm._ensure_name_index()
    assert cm._record_for_name("my-box") == "rec-9"


# ---------------------------------------------------------------------------
# (9) the CLI wrapper: argument validation, name resolution, JSON shape
# ---------------------------------------------------------------------------


_CLI_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "reap_container_record.py")


def _load_cli():
    spec = importlib.util.spec_from_file_location("reap_container_record_cli",
                                                  _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cli():
    return _load_cli()


def test_cli_rejects_both_record_and_name(cli):
    assert cli.main(["--workspace", _WS, "--record", "rec-1",
                     "--name", "box"]) == 2


def test_cli_rejects_neither_record_nor_name(cli):
    assert cli.main(["--workspace", _WS]) == 2


def test_cli_ambiguous_name_is_refused_with_candidates(cli, monkeypatch, capsys):
    _mint(_WS, "rec-a", name="dup")
    _mint(_WS, "rec-b", name="dup")

    def _factory(*args, **kwargs):
        cm = ContainerManager.__new__(ContainerManager)
        cm.workspace_id = kwargs.get("workspace_id")
        return cm

    monkeypatch.setattr(cli, "ContainerManager", _factory)

    rc = cli.main(["--workspace", _WS, "--name", "dup"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "AMBIGUOUS" in err
    assert "rec-a" in err and "rec-b" in err


def test_cli_json_apply_shape(cli, monkeypatch, capsys):
    _mint(_WS, "rec-1", docker_id="d" * 16, age_days=10)
    monkeypatch.setattr(container_manager, "docker", _FakeDocker([]))
    monkeypatch.setattr(container_manager, "DOCKER_AVAILABLE", True)
    monkeypatch.setattr(container_manager, "_audit", lambda event, data: None)
    monkeypatch.setattr(container_manager, "log", lambda *a, **k: None)

    rc = cli.main(["--workspace", _WS, "--record", "rec-1", "--apply", "--json"])
    assert rc == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert isinstance(payload, dict)
    assert payload["applied"] is True
    assert payload["reason"] == "ok"
    assert payload["exit_code"] == 0


# ---------------------------------------------------------------------------
# (7b) class gate: EPHEMERAL records ARE force-reaped (they do not own their
#      lifecycle); RESOURCE / SERVICE are still refused (lifecycle_own).
# ---------------------------------------------------------------------------


def test_ephemeral_record_is_force_reaped():
    path = _mint(_WS, "rec-1", lifecycle_class=LIFECYCLE_EPHEMERAL,
                 docker_id="d" * 16, age_days=0)
    result = reap_container_record(
        _WS, "rec-1", apply=True, docker_client=_FakeDocker([]))
    assert result["found"] is True
    assert result["reaped"] is True
    assert result["applied"] is True
    assert result["reason"] == "ok"
    assert not path.is_file()


@pytest.mark.parametrize("lifecycle_class",
                         [LIFECYCLE_RESOURCE, LIFECYCLE_SERVICE])
def test_own_lifecycle_classes_are_refused_by_force_reap(lifecycle_class):
    path = _mint(_WS, "rec-1", lifecycle_class=lifecycle_class,
                 docker_id="d" * 16, age_days=10)
    result = reap_container_record(
        _WS, "rec-1", apply=True, docker_client=_FakeDocker([]))
    assert result["reaped"] is False
    assert result["reason"] == "lifecycle_own"
    assert path.is_file()

