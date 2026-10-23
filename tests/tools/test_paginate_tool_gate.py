"""Tests for the ``PaginateTool`` wrapped-call permission re-gate.

``PaginateTool`` resolves a target tool class out of the registry and
constructs it itself.  The executor only gates the tool the agent actually
asked for -- ``PaginateTool``, which requires ``filesystem:read`` -- so a
wrapped class the session could never call directly (a container control, an
arbitrary host shell) used to run anyway with the caller's own
``effective_permissions``: an authority-laundering hop.

These tests pin the re-gate: the wrapped class's
``get_required_categories()`` is checked against the caller's effective
permissions *before* the wrapped tool is instantiated, and the
executor-owned ``workspace_id`` is never forwarded from caller-supplied
``tool_params``.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import pytest

import tools as tools_pkg
from tools.base import ToolBase
from tools.container_control import ContainerRemoveTool
from tools.glob_tool import GlobTool
from tools.host_bash_tool import HostBashTool
from tools.paginate_tool import PaginateTool

#: The stock/default session profile (see agent/config/defaults.py):
#: filesystem read-only, containers false, host bash absent (=> banned).
STOCK_PERMISSIONS = {"filesystem": "read", "container": False}


class _WorkspaceWitness(ToolBase):
    """Wrapped-tool stand-in that *does* declare ``workspace_id``."""

    tool: Literal["_WorkspaceWitness"] = "_WorkspaceWitness"
    workspace_id: str = ""
    received: ClassVar[list] = []

    def execute(self) -> str:
        type(self).received.append(self)
        return "witness-ok"


@pytest.fixture(autouse=True)
def _reset_witness_records():
    _WorkspaceWitness.received.clear()
    yield
    _WorkspaceWitness.received.clear()


@pytest.fixture
def stub_registry(monkeypatch):
    """Replace the tool registry with the given stub(s)."""

    def _install(*classes):
        monkeypatch.setattr(tools_pkg, "TOOL_CLASSES", list(classes))

    return _install


def _forbid_instantiation(monkeypatch, tool_class):
    """Make ``tool_class(**kwargs)`` explode, recording any attempt."""

    attempts = []

    def _boom(self, *args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("instantiated")

    monkeypatch.setattr(tool_class, "__init__", _boom, raising=True)
    return attempts


# -- A wrapped tool outside the caller's categories is refused -----------


def test_paginate_refuses_wrapped_tool_outside_caller_categories(monkeypatch):
    """A stock filesystem-only session cannot reach container control."""
    attempts = _forbid_instantiation(monkeypatch, ContainerRemoveTool)

    result = PaginateTool(
        target_tool_name="ContainerRemoveTool",
        tool_params={"container_id": "x", "workspace_id": "victim-ws"},
        effective_permissions=dict(STOCK_PERMISSIONS),
    ).execute()

    assert isinstance(result, str)
    assert "not permitted" in result
    assert "ContainerRemoveTool" in result
    assert attempts == [], f"wrapped tool was instantiated: {attempts}"


def test_paginate_refuses_wrapped_host_bash_for_stock_session(monkeypatch):
    """A stock session (host_bash absent => banned) cannot reach a shell."""
    attempts = _forbid_instantiation(monkeypatch, HostBashTool)

    result = PaginateTool(
        target_tool_name="HostBashTool",
        tool_params={"command": "id"},
        effective_permissions=dict(STOCK_PERMISSIONS),
    ).execute()

    assert isinstance(result, str)
    assert "not permitted" in result
    assert "HostBashTool" in result
    assert attempts == [], f"wrapped tool was instantiated: {attempts}"


# -- Allowed wrapped calls still work (no regression) --------------------


def test_paginate_works_within_caller_categories(tmp_path):
    """``filesystem:read`` is enough to wrap ``GlobTool``; it still runs."""
    (tmp_path / "inside.py").write_text("", encoding="utf-8")

    result = PaginateTool(
        target_tool_name="GlobTool",
        tool_params={"directory": str(tmp_path), "pattern": "*.py"},
        workspace_path=str(tmp_path),
        effective_permissions=dict(STOCK_PERMISSIONS),
    ).execute()

    assert not result.startswith("Error:"), result
    assert "inside.py" in result


# -- The executor-owned workspace_id is never caller-forgeable ----------


def test_paginate_does_not_forward_forged_workspace_id(stub_registry):
    """``tool_params['workspace_id']`` must be dropped, not forwarded."""
    stub_registry(_WorkspaceWitness)

    result = PaginateTool(
        target_tool_name="_WorkspaceWitness",
        tool_params={"workspace_id": "victim-ws"},
        effective_permissions=dict(STOCK_PERMISSIONS),
    ).execute()

    assert result == "witness-ok"
    assert len(_WorkspaceWitness.received) == 1
    assert _WorkspaceWitness.received[0].workspace_id != "victim-ws"
    assert _WorkspaceWitness.received[0].workspace_id == ""


# -- Control-plane / meta targets are refused structurally ---------------

_CONTROL_PLANE_NAMES = sorted(PaginateTool._NEVER_PAGINATE)


def _resolve_real(name):
    """Find a class in the real registry, or None (no patch possible)."""
    for cls in tools_pkg.TOOL_CLASSES:
        if cls.__name__ == name:
            return cls
    return None


@pytest.mark.parametrize("target_name", _CONTROL_PLANE_NAMES)
def test_paginate_refuses_control_plane_targets(monkeypatch, target_name):
    """No control-plane tool may be reached through pagination.

    These tools cannot be gated by categories (several declare none), so the
    refusal is structural and happens before the registry scan.  Where the
    class is resolvable we also prove it was never instantiated.
    """
    patched = _resolve_real(target_name)
    # PaginateTool cannot patch its own __init__ (the test has to construct
    # it), and a name that is not in the registry has nothing to patch.
    attempts = (
        _forbid_instantiation(monkeypatch, patched)
        if patched is not None and patched is not PaginateTool
        else None
    )

    result = PaginateTool(
        target_tool_name=target_name,
        tool_params={},
        effective_permissions=dict(STOCK_PERMISSIONS),
    ).execute()

    assert isinstance(result, str)
    assert "not permitted" in result
    assert target_name in result
    if attempts is not None:
        assert attempts == [], f"{target_name} was instantiated: {attempts}"


def test_paginate_refusal_does_not_enumerate_registry():
    """The not-found refusal must not hand back the tool registry."""
    result = PaginateTool(
        target_tool_name="NoSuchToolFoo",
        effective_permissions=dict(STOCK_PERMISSIONS),
    ).execute()

    assert result.startswith("Error: Tool 'NoSuchToolFoo' not found.")
    assert "Available tools" not in result
    assert "GlobTool" not in result
    assert "ContainerRemoveTool" not in result
    assert "[" not in result
