"""Tests for ContainerStatusTool lifecycle/ownership description fields.

Wave 9b PHASE 2 adds five declarative keys to the ``ContainerStatusTool``
response so callers can tell who owns a container, what lifecycle class it
belongs to, how it was created, how long it will live, and which grain is
needed to remove it:

    owner, lifecycle_class, created_by, ttl_remaining_s, grain_required_for_remove

These tests pin the *shape* and the *rules* of that description without
touching Docker: ``_make_manager`` is patched and the manager is a MagicMock.
"""

import json
from unittest.mock import MagicMock, patch

from tools.container_control import ContainerStatusTool

# Canonical happy-path payload returned by ``manager.status()``.
RUNNING = {
    "container_id": "abc123",
    "name": "tm-x",
    "status": "running",
    "uptime_seconds": 12,
    "memory_usage_bytes": 1024,
    "note": "",
    "oom_killed": False,
}

# The description keys every non-missing status response carries
# (the five PHASE-2 keys plus Wave 10a's ``unavailable_reason``).
DESCRIPTION_KEYS = {
    "owner",
    "lifecycle_class",
    "created_by",
    "ttl_remaining_s",
    "grain_required_for_remove",
    "unavailable_reason",
}


def _manager(status_result=None, lifecycle_class="ephemeral", labels=None):
    """Build a MagicMock container manager.

    ``labels`` defaults to ``{}`` — an empty label map guarantees the tool
    short-circuits before any vault / container-record lookup, so the tests
    stay hermetic.
    """
    manager = MagicMock()
    manager.status.return_value = dict(status_result or RUNNING)
    manager.class_of.return_value = lifecycle_class
    # The persistent path now consults manager.unavailable_reason(); pin it to
    # a JSON-serializable value (the real manager returns str | None).
    manager.unavailable_reason.return_value = None
    container = MagicMock()
    container.name = "tm-x"
    container.labels = dict(labels or {})
    manager.client.containers.get.return_value = container
    return manager


def _run(manager, **kwargs):
    """Execute ContainerStatusTool with ``_make_manager`` patched to manager."""
    tool = ContainerStatusTool(container_id="abc123", **kwargs)
    with patch.object(ContainerStatusTool, "_make_manager", return_value=manager):
        return json.loads(tool.execute())


# ---------------------------------------------------------------------------
# Description rules
# ---------------------------------------------------------------------------


def test_ephemeral_container_description():
    manager = _manager(lifecycle_class="ephemeral")
    result = _run(manager, session_id="sess-1")

    assert result["success"] is True
    # The underlying status payload is preserved verbatim.
    assert result["status"] == "running"
    assert result["uptime_seconds"] == 12
    # An ephemeral container is owned by the session that spawned it.
    assert result["owner"] == "sess-1"
    assert result["lifecycle_class"] == "ephemeral"
    assert result["created_by"] == "unknown"
    assert result["ttl_remaining_s"] is None
    assert result["grain_required_for_remove"] == "container:true"


def test_ephemeral_without_session_owned_by_workspace():
    manager = _manager(lifecycle_class="ephemeral")
    result = _run(manager)

    assert result["success"] is True
    assert result["owner"] == "workspace"
    assert result["grain_required_for_remove"] == "container:true"


def test_persistent_container_description():
    manager = _manager(lifecycle_class="persistent")
    result = _run(manager, session_id="sess-1")

    assert result["success"] is True
    # A persistent container belongs to the workspace, not a session.
    assert result["owner"] == "workspace"
    assert result["lifecycle_class"] == "persistent"
    # Persistent removal is operator-only: the agent path refuses it, so no
    # session grain is ever sufficient.
    assert result["grain_required_for_remove"] == "operator"


def test_resource_container_description():
    manager = _manager(lifecycle_class="resource")
    result = _run(manager, session_id="sess-1")

    assert result["success"] is True
    assert result["owner"] == "operator"
    assert result["lifecycle_class"] == "resource"
    assert result["grain_required_for_remove"] == "operator"


def test_unclassifiable_container_fails_closed():
    # No lifecycle class -> the base grain is advertised; the funnel refuses.
    manager = _manager(lifecycle_class=None)
    result = _run(manager)

    assert result["success"] is True
    assert result["lifecycle_class"] is None
    assert result["grain_required_for_remove"] == "container:true"


def test_missing_container_reports_null_description():
    manager = _manager(status_result={"container_id": "abc123", "status": "missing"})
    result = _run(manager)

    assert result["success"] is True
    assert result["status"] == "missing"
    for key in DESCRIPTION_KEYS:
        assert key in result, key
        assert result[key] is None, key


def test_description_survives_lookup_and_classification_failure():
    # The container cannot be re-resolved by the manager; the tool must still
    # return a well-formed description rather than raising.
    manager = _manager(lifecycle_class=None)
    manager.client.containers.get.side_effect = RuntimeError("daemon down")
    result = _run(manager)

    assert result["success"] is True
    assert result["status"] == "running"
    assert result["lifecycle_class"] is None
    assert result["grain_required_for_remove"] == "container:true"


def test_status_response_always_carries_description_keys():
    manager = _manager(lifecycle_class="ephemeral")
    result = _run(manager)

    for key in DESCRIPTION_KEYS:
        assert key in result, key


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_manager_construction_failure_reports_error():
    tool = ContainerStatusTool(container_id="abc123")
    with patch.object(
        ContainerStatusTool, "_make_manager", side_effect=RuntimeError("no docker")
    ):
        result = json.loads(tool.execute())

    assert result["success"] is False
    assert result["status"] == "error"
    assert "no docker" in result["error"]


def test_status_lookup_failure_reports_error():
    manager = _manager()
    manager.status.side_effect = RuntimeError("boom")
    result = _run(manager)

    assert result["success"] is False
    assert "boom" in result["error"]
