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


def _show_file_tool(tmp_path, effective, source="working", **overrides):
    """A ``show_file`` tool reading ``a.txt`` out of ``tmp_path``.

    ``effective_permissions`` is assigned AFTER construction for the same reason
    as :func:`_git_read_tool`; path validation is stubbed to the identity so the
    grain gate -- not the workspace registry -- is what the test exercises.
    """
    params = {
        "operation": "show_file",
        "path": "a.txt",
        "source": source,
        "session_id": "s",
        "workspace_id": "w",
        "session_permissions": {},
    }
    params.update(overrides)
    tool = GitReadTool(**params)
    tool.effective_permissions = effective
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "w")
    object.__setattr__(tool, "_validate_path", lambda p: str(p))
    return tool


def test_show_file_working_refused_when_filesystem_banned(tmp_path, monkeypatch):
    """``show_file source="working"`` reads DISK, so a banned filesystem grain
    refuses it outright -- loudly, naming the grain, with nothing read and no
    silent fallback into the committed blob."""
    (tmp_path / "a.txt").write_text("SECRET-DISK-BYTES\n", encoding="utf-8")
    tool = _show_file_tool(tmp_path, {"git": "read", "filesystem": "banned"})

    def _no_io(*args, **kwargs):  # pragma: no cover - the assertion IS the test
        raise AssertionError(
            "the working tree was read under a banned filesystem grain"
        )

    monkeypatch.setattr(Path, "open", _no_io)
    monkeypatch.setattr(Path, "read_text", _no_io)
    monkeypatch.setattr(Path, "read_bytes", _no_io)

    out = tool._git_show_file(tmp_path)

    assert out.startswith("Error:"), out
    assert "filesystem:read denied" in out, out
    assert "filesystem:banned" in out, (
        f"the denial must name the grain the session actually has: {out!r}"
    )
    assert "Nothing was read" in out, out
    assert "SECRET-DISK-BYTES" not in out, out


def test_show_file_working_reads_disk_when_filesystem_read(tmp_path):
    """A read-capable filesystem grain lets the same call read the disk bytes."""
    (tmp_path / "a.txt").write_text("DISK-BYTES\n", encoding="utf-8")
    tool = _show_file_tool(tmp_path, {"git": "read", "filesystem": "read"})

    out = tool._git_show_file(tmp_path)

    assert out.splitlines()[0].startswith("source: working"), out
    assert "DISK-BYTES" in out, out


def test_show_file_committed_unaffected_when_filesystem_banned(tmp_path):
    """The gate above is the WORKING-TREE one: the committed read dispatches to
    git unchanged (its own grain binding lives in the exec paths)."""
    tool = _show_file_tool(
        tmp_path, {"git": "read", "filesystem": "banned"}, source="committed"
    )
    argv: list = []

    def fake_run_git_raw(repo_root, args, timeout=30):
        argv.append(list(args))
        return (0, "HEAD-BLOB\n", "")

    object.__setattr__(tool, "_run_git_raw", fake_run_git_raw)

    out = tool._git_show_file(tmp_path)

    assert argv == [["show", "--no-ext-diff", "--no-textconv", "HEAD:a.txt"]], (
        f"the committed read must still be one <ref>:<path> show: {argv!r}"
    )
    assert out.splitlines()[0].startswith("source: committed"), out
    assert "HEAD-BLOB" in out, out


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
