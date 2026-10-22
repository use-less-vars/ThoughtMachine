"""Tests for ``PaginateTool`` context propagation (``tools/paginate_tool.py``).

``PaginateTool`` constructs the wrapped tool itself, so it must hand it the
same executor-injected context a direct call would receive (workspace_path,
session/effective permissions, session id, worker flag, ...).  Before the fix
it forwarded only ``tool_params`` plus an unconditional ``page``/``per_page``,
which meant:

* wrapping any non-paginating tool raised
  ``ValidationError: extra_forbidden`` (``ToolBase`` uses ``extra='forbid'``),
  surfacing as ``Error executing <tool>: ...``;
* wrapping a tool that *does* resolve the workspace got ``workspace_path=None``,
  so the wrapped tool lost both its workspace scope and any containment check
  built on it.

These tests pin both halves: pagination is injected only where the wrapped
class declares it, and the injected context reaches the wrapped tool.
"""

from __future__ import annotations

import json
from typing import ClassVar, Literal

import pytest

import tools as tools_pkg
from tools.base import ToolBase
from tools.paginate_tool import PaginateTool


class _NonPaginatingStub(ToolBase):
    """Wrapped-tool stand-in that declares no ``page``/``per_page`` fields."""

    tool: Literal["_NonPaginatingStub"] = "_NonPaginatingStub"
    received: ClassVar[list] = []

    def execute(self) -> str:
        type(self).received.append(self)
        return "stub-ok"


class _PaginatingStub(ToolBase):
    """Wrapped-tool stand-in that declares the pagination fields."""

    tool: Literal["_PaginatingStub"] = "_PaginatingStub"
    page: int = 1
    per_page: int = 100
    received: ClassVar[list] = []

    def execute(self) -> str:
        type(self).received.append(self)
        return "stub-ok"


@pytest.fixture(autouse=True)
def _reset_stub_records():
    _NonPaginatingStub.received.clear()
    _PaginatingStub.received.clear()
    yield
    _NonPaginatingStub.received.clear()
    _PaginatingStub.received.clear()


@pytest.fixture
def stub_registry(monkeypatch):
    """Replace the tool registry with the given stub(s)."""

    def _install(*classes):
        monkeypatch.setattr(tools_pkg, "TOOL_CLASSES", list(classes))

    return _install


# ── Pagination is injected only where it is declared ───────────────────────


def test_non_paginating_tool_is_wrapped_without_extra_forbidden(stub_registry):
    stub_registry(_NonPaginatingStub)

    result = PaginateTool(target_tool_name="_NonPaginatingStub").execute()

    assert result == "stub-ok"
    assert "extra_forbidden" not in result
    assert len(_NonPaginatingStub.received) == 1
    wrapped_fields = set(type(_NonPaginatingStub.received[0]).model_fields)
    assert "page" not in wrapped_fields
    assert "per_page" not in wrapped_fields


def test_paginating_tool_receives_page_and_per_page(stub_registry):
    stub_registry(_PaginatingStub)

    result = PaginateTool(
        target_tool_name="_PaginatingStub", page=2, per_page=5
    ).execute()

    assert result == "stub-ok"
    assert _PaginatingStub.received[0].page == 2
    assert _PaginatingStub.received[0].per_page == 5


# ── Executor-injected context is forwarded ────────────────────────────────


def test_injected_context_is_forwarded_to_wrapped_tool(stub_registry):
    stub_registry(_NonPaginatingStub)
    perms = {"filesystem": "write", "container": False}

    PaginateTool(
        target_tool_name="_NonPaginatingStub",
        workspace_path="/tmp/ws",
        token_limit=1234,
        is_worker_context=True,
        session_id="sess-abc",
        session_permissions=perms,
        effective_permissions=perms,
        agent_config={"model": "test-model"},
    ).execute()

    wrapped = _NonPaginatingStub.received[0]
    assert wrapped.workspace_path == "/tmp/ws"
    assert wrapped.token_limit == 1234
    assert wrapped.is_worker_context is True
    assert wrapped.session_id == "sess-abc"
    assert wrapped.session_permissions == perms
    assert wrapped.effective_permissions == perms
    assert wrapped.agent_config == {"model": "test-model"}


def test_tool_params_still_reach_the_wrapped_tool(stub_registry):
    stub_registry(_PaginatingStub)

    PaginateTool(
        target_tool_name="_PaginatingStub",
        tool_params={"page": 9, "per_page": 9},
        page=3,
        per_page=7,
    ).execute()

    # The dedicated pagination fields are authoritative over tool_params.
    assert _PaginatingStub.received[0].page == 3
    assert _PaginatingStub.received[0].per_page == 7


def test_unknown_tool_is_reported(stub_registry):
    stub_registry(_NonPaginatingStub)

    result = PaginateTool(target_tool_name="NoSuchTool").execute()

    assert result.startswith("Error: Tool 'NoSuchTool' not found.")


# ── Real wrapped tools (registry untouched) ───────────────────────────────


def test_real_non_paginating_tool_resolves_its_workspace(tmp_path):
    """``ReadFile`` gains its workspace from the forwarded context."""
    (tmp_path / "inside.txt").write_text("inside-content", encoding="utf-8")

    result = PaginateTool(
        target_tool_name="ReadFile",
        tool_params={"file_path": "inside.txt"},
        workspace_path=str(tmp_path),
    ).execute()

    payload = json.loads(result)
    assert payload["content"] == "inside-content"
    assert "extra_forbidden" not in result


def test_real_non_paginating_tool_refuses_path_outside_workspace(tmp_path):
    """Containment of the wrapped tool survives being driven via paginate."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("OUTSIDE-SECRET", encoding="utf-8")

    result = PaginateTool(
        target_tool_name="ReadFile",
        tool_params={"file_path": "../outside.txt"},
        workspace_path=str(workspace),
    ).execute()

    payload = json.loads(result)
    assert "escapes the workspace directory" in payload["error"]
    assert "OUTSIDE-SECRET" not in result


def test_real_paginating_tool_still_paginates(tmp_path):
    """``GlobTool`` declares page/per_page, so pagination keeps working."""
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("", encoding="utf-8")

    first = PaginateTool(
        target_tool_name="GlobTool",
        tool_params={"directory": str(tmp_path), "pattern": "*.py"},
        page=1,
        per_page=2,
    ).execute()

    assert "extra_forbidden" not in first
    assert "a.py" in first
    assert "--- Page 1 of 2 (3 total results) ---" in first


# ── Injected context is authoritative over tool_params ────────────────────


def test_context_cannot_be_forged_through_tool_params(stub_registry):
    """A caller must not be able to override the injected workspace root."""
    stub_registry(_NonPaginatingStub)

    PaginateTool(
        target_tool_name="_NonPaginatingStub",
        tool_params={"workspace_path": "/home/jojo"},
        workspace_path="/tmp/real-ws",
    ).execute()

    assert _NonPaginatingStub.received[0].workspace_path == "/tmp/real-ws"


def test_forged_workspace_path_cannot_widen_glob_containment(tmp_path):
    """``GlobTool`` keeps refusing an out-of-workspace directory when forged.

    ``tool_params`` is caller-controlled, so letting it override the injected
    ``workspace_path`` would let a caller reset the wrapped tool's workspace
    root (and its ``_validate_path`` containment) to any directory.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside-secret.txt").write_text("x", encoding="utf-8")

    result = PaginateTool(
        target_tool_name="GlobTool",
        tool_params={"workspace_path": str(outside), "directory": str(outside)},
        workspace_path=str(workspace),
    ).execute()

    assert "outside-secret.txt" not in result
    assert "outside of" in result or "outside workspace" in result


def test_glob_refuses_outside_directory_when_context_is_forwarded(tmp_path):
    """Containment of the wrapped ``GlobTool`` survives the paginate hop."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside-secret.txt").write_text("x", encoding="utf-8")

    result = PaginateTool(
        target_tool_name="GlobTool",
        tool_params={"directory": str(outside)},
        workspace_path=str(workspace),
    ).execute()

    assert "outside-secret.txt" not in result
    assert result.startswith("Error: Path ")


def test_glob_still_lists_inside_the_workspace(tmp_path):
    """The refusal above is scoping, not a blanket break of GlobTool."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "inside.py").write_text("", encoding="utf-8")

    result = PaginateTool(
        target_tool_name="GlobTool",
        tool_params={"directory": str(workspace), "pattern": "*.py"},
        workspace_path=str(workspace),
    ).execute()

    assert "inside.py" in result
    assert not result.startswith("Error:")
