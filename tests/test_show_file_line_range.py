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
#: operation via two trailing lines"), and every ``show_file`` result now starts
#: with the ``source:`` line that names where the bytes came from (the default
#: source is the WORKING TREE, not the committed blob).  Both are real output,
#: so the tests strip the NAMING lines before comparing the BODY -- they do not
#: weaken the window assertions: exact line content and line counts are still
#: compared.
_TRAILER_LINE = re.compile(r"^(execution_mode|failure_reason|source):")


def _content() -> str:
    lines = [f"line-{i:03d}" for i in range(1, CONTENT_LINES + 1)]
    lines[949] = MARKER  # 0-based index 949 == line 950
    return "\n".join(lines) + "\n"


def _tool(**kwargs) -> GitReadTool:
    # This module tests the LINE WINDOW, which is source-independent; it drives
    # the committed-blob read because that is the read its fake seam supplies.
    # ``source`` is therefore explicit: the tool's default is the working tree,
    # which would never reach the faked git call.
    kwargs.setdefault("source", "committed")
    tool = GitReadTool(operation="show_file", path="big.py", **kwargs)
    tool._run_git_raw = lambda *a, **k: (0, _content(), "")
    tool._validated_rel_paths = lambda repo_root, p: [p]
    return tool


def _full_output(tool: GitReadTool) -> str:
    return tool._git_show_file(Path("/tmp"))


def _body(tool: GitReadTool) -> str:
    """Return the output with the provenance/trailer naming lines removed."""
    out = _full_output(tool)
    lines = out.splitlines()
    stripped = [line for line in lines if _TRAILER_LINE.match(line)]
    # The trailer must actually be present, or this helper would be hiding a
    # missing diagnostic rather than ignoring a known one.
    assert stripped, f"expected an execution_mode/failure_reason trailer in {out[-300:]!r}"
    # Drop the naming lines WHEREVER they appear (``source:`` is now the FIRST
    # line of the result, the trailer lines the last two), then the blank line
    # that ``_with_mode`` inserts before the trailer.
    lines = [line for line in lines if not _TRAILER_LINE.match(line)]
    while lines and not lines[-1].strip():
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
