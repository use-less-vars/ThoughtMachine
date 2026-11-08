"""``show_file`` must honour ``line_start``/``line_end``.

The handler used to hand the WHOLE blob to ``ToolBase._truncate_output``, which
keeps the head (``output[:limit*4]``).  So a caller asking for lines 950-955 of
a large file received the top of the file with a truncation notice -- a wrong
answer that reads like a right one.

These tests are hermetic: no git binary, no repository.  The git call and the
path validation are monkeypatched on the instance, so only the line-window
logic is under test.
"""

import re
from pathlib import Path

import pytest

from tools.git_info_tool import GitReadTool

CONTENT_LINES = 1200
MARKER = "MIDFILE_MARKER_950"

#: The git tools append a trailer to every result (see the class docstring
#: "Execution mode and failure diagnostics are reported per call for EVERY
#: operation via two trailing lines").  It is real output, so the tests strip it
#: before comparing the BODY -- they do not weaken the window assertions.
_TRAILER_LINE = re.compile(r"^(execution_mode|failure_reason):")


def _content() -> str:
    lines = [f"line-{i:03d}" for i in range(1, CONTENT_LINES + 1)]
    lines[949] = MARKER  # 0-based index 949 == line 950
    return "\n".join(lines) + "\n"


def _tool(**kwargs) -> GitReadTool:
    tool = GitReadTool(operation="show_file", path="big.py", **kwargs)
    tool._run_git_raw = lambda *a, **k: (0, _content(), "")
    tool._validated_rel_paths = lambda repo_root, p: [p]
    return tool


def _full_output(tool: GitReadTool) -> str:
    return tool._git_show_file(Path("/tmp"))


def _body(tool: GitReadTool) -> str:
    """Return the output with the execution-mode/failure-reason trailer removed."""
    out = _full_output(tool)
    lines = out.splitlines()
    stripped = [line for line in lines if _TRAILER_LINE.match(line)]
    # The trailer must actually be present, or this helper would be hiding a
    # missing diagnostic rather than ignoring a known one.
    assert stripped, f"expected an execution_mode/failure_reason trailer in {out[-300:]!r}"
    while lines and (not lines[-1].strip() or _TRAILER_LINE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines)


def test_range_is_sliced_not_head_truncated():
    tool = _tool(line_start=950, line_end=955)

    body = _body(tool)

    assert body.startswith(MARKER)
    assert "line-001" not in body
    assert body.splitlines() == [
        MARKER,
        "line-951",
        "line-952",
        "line-953",
        "line-954",
        "line-955",
    ]


def test_line_start_alone_returns_the_default_span():
    tool = _tool(line_start=10)

    lines = _body(tool).splitlines()

    assert len(lines) == 200
    assert lines[0] == "line-010"
    assert lines[-1] == "line-209"


def test_oversized_range_is_refused_rather_than_silently_narrowed():
    tool = _tool(line_start=1, line_end=5000, token_limit=10000)

    with pytest.raises(ValueError, match="spans 5000 lines"):
        _full_output(tool)


def test_bare_line_end_is_refused():
    tool = _tool(line_end=10)

    # A bare end has no defined start; the handler refuses instead of guessing.
    with pytest.raises(ValueError, match="has no line_start"):
        _full_output(tool)


def test_no_range_still_returns_whole_content():
    tool = _tool(token_limit=100000)

    lines = _body(tool).splitlines()

    assert lines[0] == "line-001"
    assert lines[-1] == f"line-{CONTENT_LINES}"
    assert lines[949] == MARKER
