"""Hermetic RED-first tests for bug/container-without-record-no-reaper.

``DockerExecutor._ensure_container`` is a container CREATE site.  Under the
record subsystem's invariant (a container must never exist without a record),
every create site must route through ``create_hardened_container(..., record=True)``
so the two-phase ``record_creation`` hook persists an intent stub before
``containers.run`` and attaches the Docker id afterwards.

These two tests pin that contract at the executor site:

  (a) driving ``_ensure_container`` to a create leaves a workspace container
      RECORD bound to the freshly created Docker id;
  (b) because the record exists, ``sweep_orphan_container_records`` reaps it
      once its container is gone and the record is past retention.

Everything is hermetic: the docker client is a fake (never a live daemon) and
the vault is a tmp dir via ``THOUGHTMACHINE_VAULT_ROOT``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import docker_executor
from docker_executor import DockerExecutor
import security.admission_gate as admission_gate
from security.admission_gate import Allow

import infra.container_manager as container_manager
from infra.container_manager import sweep_orphan_container_records
from thoughtmachine.container_record import (
    LIFECYCLE_PERSISTENT,
    list_records,
    load_record,
)
from thoughtmachine.container_record import storage

_WS = "ws-exec-record"


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


class _FakeCtr:
    """Minimal stand-in for the object ``containers.run`` returns."""

    def __init__(self, container_id="c-exec-1"):
        self.id = container_id
        self.name = container_id
        self.status = "running"
        self.attrs = {"State": {"Status": "running"}}

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        pass

    def remove(self, **kwargs):
        pass

    def reload(self):
        pass


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


def _drive_ensure_container(monkeypatch):
    """Drive ``DockerExecutor._ensure_container`` to the CREATE path hermetically.

    Mirrors the proven harness in ``tests/test_container_restart_policy.py``
    (site 4) plus the ``_host_ids`` pin from
    ``tests/test_container_ownership_user_fallback.py`` so it is portable inside
    a root pytest container.
    """
    try:
        from docker.errors import NotFound
    except Exception:  # pragma: no cover - docker SDK absent
        NotFound = LookupError

    client = MagicMock()
    client.containers.run.return_value = _FakeCtr("c-exec-1")
    client.containers.get.side_effect = NotFound("nope")
    client.volumes.get_or_create.return_value = MagicMock()

    monkeypatch.setattr(DockerExecutor, "_ensure_image", lambda self, *a, **k: None)
    monkeypatch.setattr(
        DockerExecutor, "_compute_container_config", lambda self: ("none", "ro")
    )
    monkeypatch.setattr(
        admission_gate, "admit", lambda request, *, probes=None: Allow(spec=request.spec)
    )
    # Pass the root-host guard (non-root host id).
    monkeypatch.setattr(docker_executor, "_host_ids", lambda: (1000, 1000))

    with patch("docker_executor.docker.from_env", return_value=client):
        executor = DockerExecutor(
            workspace_path="/tmp/tm-exec-record-ws",
            image="agent-executor-test",
            network="none",
            mem_limit="128m",
            cpu_quota=50000,
            session_permissions={"container": True},
            workspace_id=_WS,
        )

    executor._ensure_container()
    return executor


# ---------------------------------------------------------------------------
# (a) the executor create site leaves a bound container RECORD
# ---------------------------------------------------------------------------


def test_ensure_container_records_the_created_container(monkeypatch):
    _drive_ensure_container(monkeypatch)

    records = list_records(_WS)
    assert records, "DockerExecutor._ensure_container created a container without a record"

    record = records[0]
    assert record.docker_id == "c-exec-1"
    assert record.lifecycle_class == LIFECYCLE_PERSISTENT


# ---------------------------------------------------------------------------
# (b) the recorded container is reapable once gone + past retention
# ---------------------------------------------------------------------------


def test_executor_record_is_reaped_when_container_gone(monkeypatch):
    _drive_ensure_container(monkeypatch)

    records = list_records(_WS)
    assert records, "DockerExecutor._ensure_container created a container without a record"
    record = records[0]

    # Age the record past the default retention so the age gate passes.
    path = storage.record_path(_WS, record.id)
    data = storage.read_record_file(path)
    ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    data["created_at"] = ts
    data["updated_at"] = ts
    storage.write_record_file(path, data)

    # The bound container is gone (empty live set) and the workspace is registered.
    result = sweep_orphan_container_records(
        registered_workspace_ids=[_WS],
        docker_client=_FakeDocker([]),
    )

    assert result["removed"] == 1
    assert result["removed_records"] == [record.id]
    assert load_record(_WS, record.id) is None
