"""Drift guard: every declared write verb is registered at EVERY site.

``GitWriteTool`` declares its verbs in four places -- the ``operation``
``Literal``, the ``Field(description=...)`` prose, the dispatch chain in
``execute()`` and the write tuple in ``GitReadTool._get_operation_level``.
A verb that is only in the ``Literal`` is a field nobody can run; one that is
dispatched but read-level under-asks the inner permission check.  This test
reads the source instead of hard-coding the verb list, so adding a verb
without registering it everywhere fails here.

Kept in its own file (rather than next to the behavioural verb contracts in
``tests/test_git_tool_verbs.py``) because it fails for a DIFFERENT reason: it
reports the whole write surface, including verbs a given change did not touch.
"""

import re
from pathlib import Path

from tools.git_write_tool import GitWriteTool

#: The declared write-operation surface, read straight from the source so a new
#: verb cannot be added to one site and forgotten at the others.
_WRITE_TOOL_SOURCE = Path(__file__).resolve().parents[1] / "tools" / "git_write_tool.py"

_OPERATION_FIELD_RE = re.compile(
    r"operation:\s*Literal\[(?P<names>[^\]]*)\]\s*=\s*Field\((?P<desc>.*?)\n\s*\)",
    re.DOTALL,
)


def test_every_write_operation_is_registered_at_all_sites():
    """Each declared write verb is dispatched, described and write-level.

    A verb that is only in the ``Literal`` is a field nobody can run; one that
    is dispatched but read-level slips past the ``git:write`` gate.  This guard
    reads the three sites instead of hard-coding the verb list.
    """
    source = _WRITE_TOOL_SOURCE.read_text(encoding="utf-8")
    match = _OPERATION_FIELD_RE.search(source)
    assert match is not None, (
        f"could not locate the operation Literal in {_WRITE_TOOL_SOURCE}"
    )
    names = re.findall(r'"([a-z_]+)"', match.group("names"))
    assert names, f"no operation names extracted: {match.group('names')!r}"
    desc = match.group("desc")

    missing = []
    for name in names:
        if f'operation == "{name}"' not in source:
            missing.append(f"{name}: not dispatched (no `operation == {name!r}`)")
        if name not in desc:
            missing.append(f"{name}: absent from the operation description")
        level = GitWriteTool(
            session_id="s-verbs",
            workspace_id="ws-verbs",
            session_permissions={},
            operation=name,
        )._get_operation_level([])
        if level != "write":
            missing.append(f"{name}: _get_operation_level -> {level!r}")

    assert not missing, "write operations missing a registration site:\n" + "\n".join(
        missing
    )
