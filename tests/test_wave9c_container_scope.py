"""Wave 9c M3: ContainerStatusTool refuses cross-workspace ownership.

The container's ``thoughtmachine.workspace_id`` label must match the caller's
workspace; a mismatch — or an unresolvable caller workspace — fails closed so a
session can never inspect another workspace's container. These tests touch no
Docker: the manager is a MagicMock and ``_make_manager`` is patched.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

from agent.config.defaults import WORKSPACE_ID_LABEL
from tools.container_control import ContainerStatusTool

RUNNING = {"container_id": "abc123", "name": "tm-x", "status": "running"}


def _manager(workspace_id, labels):
    """Build a MagicMock container manager bound to ``workspace_id``."""
    manager = MagicMock()
    manager.workspace_id = workspace_id
    manager.status.return_value = dict(RUNNING)
    manager.class_of.return_value = "persistent"
    container = MagicMock()
    container.name = "tm-x"
    container.labels = dict(labels)
    manager.client.containers.get.return_value = container
    return manager


def _run(manager):
    tool = ContainerStatusTool(container_id="abc123")
    with patch.object(ContainerStatusTool, "_make_manager", return_value=manager):
        return json.loads(tool.execute())


def test_refuses_foreign_workspace_container():
    """A container labelled for another workspace is refused."""
    manager = _manager("ws-a", {WORKSPACE_ID_LABEL: "ws-b"})
    result = _run(manager)

    assert result["success"] is False
    assert result["status"] == "error"
    assert "belongs to workspace `ws-b`" in result["error"]
    assert "ws-a" in result["error"]


def test_refuses_when_caller_workspace_unknown():
    """An unresolvable caller workspace fails closed."""
    manager = _manager("default", {WORKSPACE_ID_LABEL: "ws-a"})
    result = _run(manager)

    assert result["success"] is False
    assert result["status"] == "error"
    assert "ownership" in result["error"]


def test_allows_same_workspace_container():
    """A container owned by the caller's workspace is reported normally."""
    manager = _manager("ws-a", {WORKSPACE_ID_LABEL: "ws-a"})
    result = _run(manager)

    assert result["success"] is True
    assert result["status"] == "running"


def test_allows_unlabeled_container():
    """An unlabeled container (no owner) has nothing to compare -> allowed."""
    manager = _manager("ws-a", {})
    result = _run(manager)

    assert result["success"] is True
    assert result["status"] == "running"


def test_warns_on_unlabeled_legacy_container(caplog):
    """M3: an unlabeled (legacy) container is allowed but logs a WARNING."""
    manager = _manager("ws-a", {})
    with caplog.at_level(logging.WARNING, logger=ContainerStatusTool.__module__):
        result = _run(manager)

    assert result["success"] is True
    assert result["status"] == "running"
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(
        "has no workspace_id label" in m and "abc123" in m for m in messages
    ), messages
