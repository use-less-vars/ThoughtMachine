"""Tests for infra.container_manager.sweep_exited_workspace_containers.

Covers the 9 Phase-3 sweep cases:
  1. exited + old           -> removed (TTL branch)
  2. exited + too young     -> kept
  3. running                -> kept
  4. resource-labelled      -> kept (never touched)
  5. registered ws          -> removed by TTL branch, NOT by orphan branch
  6. unregistered + exited + old -> removed (orphan branch)
  7. empty registry         -> nothing wiped (conservative NO-OP)
  8. daemon error           -> soft-fail, nothing raised
  9. dry_run                -> counts would-be removals, leaves in place
"""
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from infra import container_manager

WORKSPACE_LABEL = "thoughtmachine.workspace_id"
RESOURCE_LABEL = "thoughtmachine.resource"


def _iso(seconds_ago=0):
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat().replace("+00:00", "Z")


class FakeContainer:
    def __init__(self, name, status="exited", labels=None, finished_at=None,
                 remove_error=None):
        self.name = name
        self.id = name
        self.status = status
        self.labels = dict(labels or {})
        self.attrs = {"State": {"FinishedAt": finished_at or _iso(0)}}
        self.remove_calls = []
        self.remove_error = remove_error

    def remove(self, force=False):
        self.remove_calls.append({"force": force})
        if self.remove_error is not None:
            raise self.remove_error


class FakeContainers:
    def __init__(self, containers=None, list_error=None):
        self.containers = list(containers or [])
        self.list_error = list_error

    def list(self, all=False, filters=None):
        if self.list_error is not None:
            raise self.list_error
        return list(self.containers)


class FakeClient:
    def __init__(self, containers=None, list_error=None):
        self.containers = FakeContainers(containers, list_error=list_error)


def _run(containers, registered_workspace_ids=None, max_age_s=3600,
         dry_run=False, list_error=None):
    client = FakeClient(containers, list_error=list_error)
    with mock.patch("docker.from_env", return_value=client) as from_env:
        result = container_manager.sweep_exited_workspace_containers(
            registered_workspace_ids=registered_workspace_ids,
            max_age_s=max_age_s,
            dry_run=dry_run,
        )
    return result, client, from_env


def _ws_container(name, wid, **kw):
    labels = {WORKSPACE_LABEL: wid}
    labels.update(kw.pop("extra_labels", {}))
    return FakeContainer(name, labels=labels, **kw)


# 1. exited + old -> removed (TTL branch)
def test_exited_old_removed():
    c = _ws_container("c1", "ws1", finished_at=_iso(7200))
    result, client, _ = _run([c], registered_workspace_ids=["ws1"], max_age_s=3600)
    assert result["removed"] == 1
    assert result["removed_registered"] == 1
    assert result["removed_orphan"] == 0
    assert result["removed_containers"] == ["c1"]
    assert c.remove_calls == [{"force": True}]


# 2. exited + too young -> kept
def test_exited_too_young_kept():
    c = _ws_container("c1", "ws1", finished_at=_iso(600))
    result, client, _ = _run([c], registered_workspace_ids=["ws1"], max_age_s=3600)
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert c.remove_calls == []
    assert "too young" in result["detail"]


# 3. running -> kept
def test_running_kept():
    c = _ws_container("c1", "ws1", status="running", finished_at=_iso(7200))
    result, client, _ = _run([c], registered_workspace_ids=["ws1"], max_age_s=3600)
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert c.remove_calls == []


# 4. resource-labelled -> kept even when exited + old
def test_resource_labeled_kept():
    c = FakeContainer("res1", status="exited", finished_at=_iso(7200),
                      labels={WORKSPACE_LABEL: "ws1", RESOURCE_LABEL: "git"})
    result, client, _ = _run([c], registered_workspace_ids=["ws1"], max_age_s=3600)
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert c.remove_calls == []


# 5. registered ws -> TTL branch only, NOT the orphan branch
def test_registered_ws_not_removed_by_orphan_branch():
    c = _ws_container("c1", "ws1", finished_at=_iso(7200))
    result, client, _ = _run([c], registered_workspace_ids=["ws1"], max_age_s=3600)
    assert result["removed"] == 1
    assert result["removed_registered"] == 1
    assert result["removed_orphan"] == 0  # orphan branch must not claim it


# 6. unregistered + exited + old -> removed (orphan branch)
def test_unregistered_exited_old_removed():
    c = _ws_container("orphan1", "ws2", finished_at=_iso(7200))
    result, client, _ = _run([c], registered_workspace_ids=["ws1"], max_age_s=3600)
    assert result["removed"] == 1
    assert result["removed_orphan"] == 1
    assert result["removed_registered"] == 0
    assert result["removed_containers"] == ["orphan1"]
    assert c.remove_calls == [{"force": True}]


# 7. empty registry -> conservative NO-OP (docker never touched)
def test_empty_registry_nothing_wiped():
    c = _ws_container("c1", "ws1", finished_at=_iso(7200))
    result, client, from_env = _run([c], registered_workspace_ids=[], max_age_s=3600)
    assert result["removed"] == 0
    assert result["skipped"] == 0
    assert result["removed_containers"] == []
    assert "registry empty" in result["detail"]
    assert from_env.call_count == 0  # docker daemon never contacted


# 8. daemon error -> soft-fail, never raises
def test_daemon_error_soft_fail():
    result, client, _ = _run([], registered_workspace_ids=["ws1"],
                             max_age_s=3600,
                             list_error=RuntimeError("daemon down"))
    assert result["removed"] == 0
    assert result["skipped"] == 0
    assert "docker unavailable" in result["detail"]


# 9. dry_run -> counts would-be removals, leaves containers in place
def test_dry_run_leaves_in_place():
    c1 = _ws_container("c1", "ws1", finished_at=_iso(7200))
    c2 = _ws_container("orphan1", "ws2", finished_at=_iso(7200))
    result, client, _ = _run([c1, c2], registered_workspace_ids=["ws1"],
                             max_age_s=3600, dry_run=True)
    assert result["removed"] == 2
    assert result["removed_registered"] == 1
    assert result["removed_orphan"] == 1
    assert result["dry_run"] is True
    assert result["removed_containers"] == ["c1", "orphan1"]
    assert c1.remove_calls == [] and c2.remove_calls == []


# extra: registered_workspace_ids=None -> TTL-only sweep (all treated as
# registered; orphan classification disabled)
def test_none_registry_ttl_only_sweep():
    c = _ws_container("c1", "ws1", finished_at=_iso(7200))
    result, client, _ = _run([c], registered_workspace_ids=None, max_age_s=3600)
    assert result["removed"] == 1
    assert result["removed_registered"] == 1
    assert result["removed_orphan"] == 0
    assert c.remove_calls == [{"force": True}]


# extra: remove() failure is swallowed and counted as skipped
def test_remove_failure_soft_fail():
    c = _ws_container("c1", "ws1", finished_at=_iso(7200),
                      remove_error=RuntimeError("boom"))
    result, client, _ = _run([c], registered_workspace_ids=["ws1"], max_age_s=3600)
    assert result["removed"] == 0
    assert result["skipped"] == 1
    assert "remove failed" in result["detail"]
