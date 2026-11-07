"""
Wave 10a regression tests: KnowledgeBaseTool workspace-scope filesystem grain.

The workspace-scope KB obeys the ``filesystem`` grain (mirroring
``tools/git_info_tool.py``):

* ``read``/``search``/``list``/``summary``/``status`` require ``filesystem:read``
  (``filesystem:write`` is above read, so it also satisfies read);
* ``append``/``update``/``create_domain`` require ``filesystem:write``;
* a *missing* ``filesystem`` key must defer to the out-of-band session mirror
  (legacy/direct callers) and therefore be ALLOWED — this is the regression this
  module pins down (the earlier buggy gate denied whenever the key was absent);
* the global (cross-project) KB is NOT subject to the workspace filesystem gate.

Assertions drive the public ``tool.execute()`` surface with a MARKER string so
an allowed read/write is observable in the returned text / on-disk file.
"""

import agent.knowledge.global_kb as global_kb
from tools.knowledge_base import KnowledgeBaseTool

MARKER = "MARKER-KB-W10A"
ENTRY = "NEWENTRY-W10A"


def _make_kb(tmp_path):
    """Create a workspace KB with a custom ``notes`` domain under personal/."""
    personal = tmp_path / ".thoughtmachine" / "knowledge" / "personal"
    personal.mkdir(parents=True, exist_ok=True)
    (personal / "notes.md").write_text(
        f"## Domain: notes\n\n{MARKER}\n", encoding="utf-8"
    )
    return tmp_path


def _notes_file(tmp_path):
    return tmp_path / ".thoughtmachine" / "knowledge" / "personal" / "notes.md"


def _workspace_tool(tmp_path, **kw):
    kw.setdefault("workspace_path", str(tmp_path))
    kw.setdefault("scope", "workspace")
    return KnowledgeBaseTool(**kw)


def test_workspace_read_refused_when_filesystem_banned(tmp_path):
    _make_kb(tmp_path)
    tool = _workspace_tool(
        tmp_path, mode="read", domain="notes",
        effective_permissions={"filesystem": "banned"},
    )
    result = tool.execute()
    assert "filesystem:read" in result
    assert MARKER not in result


def test_workspace_write_refused_when_filesystem_read(tmp_path):
    _make_kb(tmp_path)
    tool = _workspace_tool(
        tmp_path, mode="append", domain="notes", entry=ENTRY,
        effective_permissions={"filesystem": "read"},
    )
    result = tool.execute()
    assert "filesystem:write" in result
    assert ENTRY not in _notes_file(tmp_path).read_text(encoding="utf-8")


def test_workspace_refused_write_leaves_no_kb_skeleton(tmp_path):
    """A refused workspace write must not materialise the KB skeleton.

    The skeleton (``project/``, ``personal/`` and the six template files) used
    to be created during root resolution, i.e. BEFORE the grain gate, so an
    unauthorised write still left fingerprints on disk.
    """
    kb = tmp_path / ".thoughtmachine" / "knowledge"
    tool = _workspace_tool(
        tmp_path, mode="append", domain="notes", entry=ENTRY,
        effective_permissions={"filesystem": "read"},
    )
    result = tool.execute()

    assert "filesystem:write" in result
    assert not kb.exists(), "a refused write must not create the KB skeleton"


def test_workspace_read_and_write_allowed_when_filesystem_full(tmp_path):
    _make_kb(tmp_path)
    read = _workspace_tool(
        tmp_path, mode="read", domain="notes",
        effective_permissions={"filesystem": "full"},
    ).execute()
    assert MARKER in read

    write = _workspace_tool(
        tmp_path, mode="append", domain="notes", entry=ENTRY,
        effective_permissions={"filesystem": "full"},
    ).execute()
    assert "appended" in write.lower()
    assert ENTRY in _notes_file(tmp_path).read_text(encoding="utf-8")


def test_workspace_allowed_when_filesystem_key_absent(tmp_path):
    """A missing ``filesystem`` key must NOT deny (legacy/direct callers)."""
    _make_kb(tmp_path)
    for eff in ({}, None):
        read = _workspace_tool(
            tmp_path, mode="read", domain="notes", effective_permissions=eff,
        ).execute()
        assert MARKER in read

    write = _workspace_tool(
        tmp_path, mode="append", domain="notes", entry=ENTRY,
        effective_permissions={},
    ).execute()
    assert ENTRY in _notes_file(tmp_path).read_text(encoding="utf-8")


def test_global_scope_allowed_when_filesystem_banned(tmp_path, monkeypatch):
    """The global KB is cross-project and not gated by the workspace grain."""
    gdir = tmp_path / "global"
    (gdir / "user").mkdir(parents=True, exist_ok=True)
    (gdir / "user" / "w10a_domain.md").write_text(
        f"{MARKER}-GLOBAL\n", encoding="utf-8"
    )
    monkeypatch.setattr(global_kb, "GLOBAL_KB_DIR", gdir)

    tool = KnowledgeBaseTool(
        mode="read", domain="w10a_domain", scope="global",
        effective_permissions={"filesystem": "banned"},
    )
    result = tool.execute()
    assert f"{MARKER}-GLOBAL" in result


def test_global_write_refused_when_filesystem_read(tmp_path, monkeypatch):
    """Global-scope WRITE still runs the historical atomic ``filesystem:write`` check.

    The workspace grain gate does not apply to ``scope="global"``; the refusal
    for a present-but-insufficient ``filesystem`` grant must therefore come from
    the additive ``elif mode in write_modes`` branch.  Removing that branch
    would let the write through, so this pins the no-loosening property.
    """
    gdir = tmp_path / "global"
    (gdir / "user").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(global_kb, "GLOBAL_KB_DIR", gdir)
    monkeypatch.setattr(global_kb, "SYSTEM_DIR", gdir / "system")
    monkeypatch.setattr(global_kb, "USER_DIR", gdir / "user")

    tool = KnowledgeBaseTool(
        mode="append", domain="w10a_write", entry=ENTRY, scope="global",
        effective_permissions={"filesystem": "read"},
    )
    result = tool.execute()
    assert "filesystem:write" in result
    target = gdir / "user" / "w10a_write.md"
    assert not target.exists() or ENTRY not in target.read_text(encoding="utf-8")


def test_global_write_refused_when_filesystem_key_absent(tmp_path, monkeypatch):
    """Historical back-compat pin for a missing ``filesystem`` key.

    The restored atomic check fails closed on a missing/unknown category
    (``check_atomic_operation`` returns ``False`` when the key is absent),
    exactly as it did before the workspace grain gate was introduced.  This
    assertion therefore flips to "allowed" if the additive ``elif`` branch is
    removed.
    """
    gdir = tmp_path / "global"
    (gdir / "user").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(global_kb, "GLOBAL_KB_DIR", gdir)
    monkeypatch.setattr(global_kb, "SYSTEM_DIR", gdir / "system")
    monkeypatch.setattr(global_kb, "USER_DIR", gdir / "user")

    tool = KnowledgeBaseTool(
        mode="append", domain="w10a_write", entry=ENTRY, scope="global",
        effective_permissions={},
    )
    result = tool.execute()
    assert "filesystem:write" in result
    target = gdir / "user" / "w10a_write.md"
    assert not target.exists() or ENTRY not in target.read_text(encoding="utf-8")

