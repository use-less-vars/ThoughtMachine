"""F4: the DELETE path is workspace-scoped, exactly like the READ path.

``ContainerStatusTool`` refuses to inspect a container belonging to another
workspace (``tests/test_wave9c_container_scope.py``); ``ContainerRemoveTool`` had
no equivalent, so a session bound to workspace A could remove workspace B's
containers. Two properties are pinned here:

1. the container's ``thoughtmachine.workspace_id`` label must match the caller's
   workspace, and an unresolvable caller workspace fails CLOSED -- the refusal
   names the layer that fired and precedes the lifecycle-class refusals;
2. the workspace id is an AUTHORITY FACT like ``session_id``: it is not a
   model-visible argument, a caller-supplied value is dropped before validation,
   and the executor's own id is what the tool receives.

These tests touch no Docker: the manager is a MagicMock and ``_make_manager`` is
patched.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agent.config.defaults import WORKSPACE_ID_LABEL
from tools.container_control import ContainerRemoveTool


def _manager(workspace_id, labels, lifecycle_class="ephemeral"):
    """Build a MagicMock container manager bound to ``workspace_id``."""
    manager = MagicMock()
    manager.workspace_id = workspace_id
    manager.class_of.return_value = lifecycle_class
    manager.remove.return_value = {"status": "removed", "name": "tm-x"}
    container = MagicMock()
    container.name = "tm-x"
    container.labels = dict(labels)
    manager.client.containers.get.return_value = container
    return manager


def _run(manager, **kwargs):
    tool = ContainerRemoveTool(container_id_or_name="abc123", **kwargs)
    with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
        return json.loads(tool.execute())


# ── (a) another workspace's container is REFUSED, and the message names why ──


def test_refuses_foreign_workspace_container():
    """A container labelled for another workspace is never removed."""
    manager = _manager("ws-a", {WORKSPACE_ID_LABEL: "ws-b"})
    result = _run(manager)

    assert result["success"] is False
    assert result["status"] == "refused"
    assert "belongs to workspace `ws-b`" in result["reason"]
    assert "`ws-a`" in result["reason"]
    manager.remove.assert_not_called()


def test_ownership_refusal_precedes_the_lifecycle_class_refusals():
    """The ownership layer runs FIRST, so the reason says which layer fired.

    A foreign PERSISTENT container must be refused for OWNERSHIP: if the class
    branch ran first the operator-only message would replace the truth.
    """
    manager = _manager(
        "ws-a", {WORKSPACE_ID_LABEL: "ws-b"}, lifecycle_class="persistent"
    )
    result = _run(manager)

    assert result["status"] == "refused"
    assert "belongs to workspace `ws-b`" in result["reason"]
    assert "operator" not in result["reason"].lower()
    manager.remove.assert_not_called()


# ── (c) an unresolvable caller workspace fails CLOSED ────────────────────────


def test_refuses_when_caller_workspace_unknown():
    """No resolvable caller workspace is a REFUSAL, never a permissive default."""
    manager = _manager("default", {WORKSPACE_ID_LABEL: "ws-b"})
    result = _run(manager)

    assert result["success"] is False
    assert result["status"] == "refused"
    assert "cannot verify container ownership" in result["reason"]
    manager.remove.assert_not_called()


# -- (e) an UNLABELLED container fails CLOSED: delete is not read -------------


def test_refuses_unlabelled_container():
    """No workspace label means ownership is UNVERIFIABLE, so refuse.

    The read path lets an unlabelled legacy container through (inspecting one is
    harmless); delete must not: without the label there is no way to tell our
    container from another workspace's, and a delete cannot be undone.
    """
    for labels in ({}, {"thoughtmachine.container_name": "tm-x"}):
        manager = _manager("ws-a", labels)
        result = _run(manager)

        assert result["success"] is False, labels
        assert result["status"] == "refused", labels
        assert "cannot verify container ownership" in result["reason"], labels
        assert "no workspace label" in result["reason"], labels
        manager.remove.assert_not_called()


# ── (b) the workspace id is a pinned authority fact, not an argument ─────────


def test_workspace_id_is_not_a_model_visible_argument():
    """The model is never told it may choose the workspace."""
    properties = ContainerRemoveTool.model_json_schema()["properties"]
    assert "workspace_id" not in properties


class _FakeConfig:
    workspace_path = None
    worktree_root = None
    tool_output_token_limit = None
    session_permissions = None
    permission_footprint = None


class _FakeState:
    security_config = None


def _execute_through_executor(executor_workspace_id, arguments):
    """Run the tool through ToolExecutor's real argument pipeline.

    Returns the ``workspace_id`` the tool handed to ``_make_manager``. The
    permission gate is stubbed (the property under test is the pin, not the
    gate) and ``_make_manager`` is captured instead of touching Docker.
    """
    from agent.core import tool_executor as tool_executor_module

    captured = {}

    def _fake_make_manager(self, workspace_id=None):
        captured["workspace_id"] = workspace_id
        manager = MagicMock()
        manager.workspace_id = workspace_id
        manager.client.containers.get.side_effect = RuntimeError("absent")
        return manager

    executor = tool_executor_module.ToolExecutor(
        tool_classes=[ContainerRemoveTool],
        config=_FakeConfig(),
        state=_FakeState(),
        logger=None,
        security_available=False,
        agent=None,
    )
    with patch.object(
        tool_executor_module,
        "check_required_categories",
        lambda *a, **k: (True, ""),
    ), patch.object(tool_executor_module, "GATE_AVAILABLE", True), patch.object(
        ContainerRemoveTool, "_make_manager", _fake_make_manager
    ):
        executor._execute_single_tool(
            ContainerRemoveTool,
            dict(arguments),
            "ContainerRemoveTool",
            0,
            lambda: False,
            lambda: None,
            lambda: 0,
            session_id="sess-a",
            workspace_id=executor_workspace_id,
        )
    return captured.get("workspace_id", "NOT_INJECTED")


def test_executor_pin_wins_over_a_disagreeing_caller_argument():
    """A caller-supplied workspace_id can never win against the executor."""
    received = _execute_through_executor(
        "ws-a", {"container_id_or_name": "abc123", "workspace_id": "ws-b"}
    )
    assert received == "ws-a"


def test_caller_supplied_workspace_id_is_dropped_when_the_executor_has_none():
    """With no id to inject the caller's value is still dropped -- never used."""
    received = _execute_through_executor(
        "", {"container_id_or_name": "abc123", "workspace_id": "ws-b"}
    )
    assert received is None


# ── (d) negative control: the fix cannot pass by refusing everything ─────────


def test_removes_container_in_its_own_workspace():
    manager = _manager("ws-a", {WORKSPACE_ID_LABEL: "ws-a"})
    result = _run(manager)

    assert result["success"] is True
    assert result["status"] == "removed"
    manager.remove.assert_called_once_with("abc123", keep_record=False)
