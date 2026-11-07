"""Wave 10a Commit 4 — git read bound to the effective ``filesystem`` grain.

The git read path is bound to the effective ``filesystem`` grain: an explicit
``filesystem: banned`` refuses the git read (host and container paths) with a
message naming ``filesystem:read``; a missing ``filesystem`` key defers
(legacy/direct callers) and a read-capable grain proceeds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.git_info_tool import GitReadTool


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _git_read_tool(effective):
    """Build a GitReadTool with a post-construction effective dict.

    ``effective_permissions`` is assigned AFTER construction (ToolExecutor
    pattern): a pydantic ``__init__`` copy would strip dict subclasses such as
    the ceiling-annotated mapping, and the executor injects it per call.
    """
    tool = GitReadTool(
        operation="status",
        session_id="s",
        workspace_id="w",
        session_permissions={},  # non-None -> in-tool gate active
    )
    tool.effective_permissions = effective
    return tool


# ---------------------------------------------------------------------------
# Commit 4 — git read bound to the effective filesystem grain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("grain", ["read", "write", "full"])
def test_filesystem_read_allowed_for_read_capable_grains(grain):
    tool = _git_read_tool({"git": "read", "filesystem": grain})
    assert tool._filesystem_read_allowed() is True


def test_filesystem_read_allowed_when_key_missing_defers():
    # Legacy / direct callers carry no filesystem key -> preserve historical
    # behaviour instead of hard-denying.
    tool = _git_read_tool({"git": "ask"})
    assert tool._filesystem_read_allowed() is True


def test_filesystem_read_banned_is_denied():
    tool = _git_read_tool({"git": "read", "filesystem": "banned"})
    assert tool._filesystem_read_allowed() is False


def test_container_exec_refuses_when_filesystem_banned():
    tool = _git_read_tool({"git": "read", "filesystem": "banned"})
    with pytest.raises(PermissionError) as excinfo:
        tool._exec_container_raw(Path("."), ["status"], manager=object())

    msg = str(excinfo.value)
    assert "filesystem:read" in msg
    assert "filesystem:banned" in msg


def test_host_exec_refuses_when_filesystem_banned():
    tool = _git_read_tool({"git": "read", "filesystem": "banned"})
    with pytest.raises(PermissionError) as excinfo:
        tool._exec_host_raw(Path("."), ["status"])

    assert "filesystem:read" in str(excinfo.value)


def test_container_exec_proceeds_when_filesystem_read():
    tool = _git_read_tool({"git": "read", "filesystem": "read"})
    # The filesystem gate must NOT fire; the call proceeds past it and only
    # fails later at the (dummy) manager layer for an unrelated reason.
    try:
        tool._exec_container_raw(Path("."), ["status"], manager=object())
    except PermissionError as exc:  # pragma: no cover - defensive
        assert "filesystem:read" not in str(exc)
    except Exception:
        pass
