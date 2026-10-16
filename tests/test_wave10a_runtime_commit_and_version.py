"""Wave 10a Commit 2 — runtime ``version`` query + container ``runtime_commit``.

Covers the new ``version`` CheckSystem query exposing the runtime
``{revision, branch, dirty}`` payload, and the ``runtime_commit`` field on the
``ContainerStatusTool`` description.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tools.container_control import ContainerStatusTool, _runtime_commit
from tools.workspace.check_system import CheckSystem

BASE_KWARGS = {"session_permissions": {"filesystem": "read", "network": "write"}}


# ---------------------------------------------------------------------------
# Commit 2 — runtime version query + container runtime_commit
# ---------------------------------------------------------------------------

def test_version_query_shape():
    tool = CheckSystem(query="version", **BASE_KWARGS)
    out = tool._query_version()

    assert set(out) == {"revision", "branch", "dirty"}
    assert isinstance(out["revision"], str)
    assert isinstance(out["branch"], str)
    assert isinstance(out["dirty"], bool)


def test_version_query_dispatchable_through_execute():
    with patch.object(CheckSystem, "_load_allowlist_from_vault", return_value=["version"]):
        tool = CheckSystem(query="version", **BASE_KWARGS)
        out = json.loads(tool.execute())

    assert set(out) == {"revision", "branch", "dirty"}
    assert isinstance(out["revision"], str)
    assert isinstance(out["branch"], str)
    assert isinstance(out["dirty"], bool)


def test_runtime_commit_helper_returns_str():
    assert isinstance(_runtime_commit(), str)


def _status_manager(status_result, lifecycle_class="ephemeral"):
    manager = MagicMock()
    manager.status.return_value = dict(status_result)
    manager.class_of.return_value = lifecycle_class
    container = MagicMock()
    container.name = "tm-x"
    container.labels = {}
    manager.client.containers.get.return_value = container
    return manager


def test_container_status_description_includes_runtime_commit():
    running = {
        "container_id": "abc123",
        "name": "tm-x",
        "status": "running",
        "uptime_seconds": 12,
        "memory_usage_bytes": 1024,
        "note": "",
        "oom_killed": False,
    }
    manager = _status_manager(running)
    tool = ContainerStatusTool(container_id="abc123")
    with patch.object(ContainerStatusTool, "_make_manager", return_value=manager):
        out = json.loads(tool.execute())

    assert "runtime_commit" in out
    assert isinstance(out["runtime_commit"], str)


def test_missing_container_runtime_commit_is_none():
    manager = _status_manager({"container_id": "abc123", "status": "missing"})
    tool = ContainerStatusTool(container_id="abc123")
    with patch.object(ContainerStatusTool, "_make_manager", return_value=manager):
        out = json.loads(tool.execute())

    assert out["status"] == "missing"
    assert out["runtime_commit"] is None
