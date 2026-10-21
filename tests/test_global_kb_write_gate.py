"""Write-gate and read-path regression tests for the GLOBAL knowledge base scope.

Covers the global-scope defects fixed in :class:`tools.knowledge_base.KnowledgeBaseTool`:

* ``update`` on a ``system/`` domain must be refused, must leave the file
  byte-identical, and must not create a same-named ``user/`` file (this is the
  regression guard for the write bypass: ``update`` used to resolve through the
  *read* path and then write to ``system/<domain>.md``).
* ``append`` on a ``system/`` domain: same three assertions.
* ``read`` on a ``system/`` domain must return the document content -- it used
  to pass ``for_write=True`` to the resolver and get the read-only refusal
  instead of the text.
* global write targets are validated as single path components (no ``/``,
  ``\\`` or ``..``), so a write can never escape ``user/``.
* the global KB is READ-ONLY for agents: ``append``/``update``/``create_domain``
  are refused even for a ``user/`` domain, whatever the caller's grant, and no
  side effect is left on disk.
* the global ``list`` output advertises only the read/search/status modes and
  names ``~/.thoughtmachine/global/`` (not the workspace path).

Hermetic: every test builds a throw-away global KB root under ``tmp_path`` and
monkeypatches the two root seams the tool uses
(``tools.knowledge_base._get_global_kb_root`` for write modes and
``agent.knowledge.global_kb.GLOBAL_KB_DIR`` for read modes), so the real
``~/.thoughtmachine/global/`` is never read or written.
"""

from pathlib import Path

import pytest

import tools.knowledge_base as kb_module
from tools.knowledge_base import KnowledgeBaseTool

DOMAIN = "handbook"
MARKER = "SYSTEM-ONLY-MARKER-42"
SYSTEM_CONTENT = (
    "# Handbook\n"
    "\n"
    "Intro for the handbook.\n"
    "\n"
    "## Rules\n"
    "\n"
    f"system rules body {MARKER}\n"
)

# Pinned verbatim: the system-domain class of refusal. The ``user/`` class must
# never converge onto this wording -- the two classes stay distinguishable.
SYSTEM_REFUSAL = (
    f"Domain '{DOMAIN}' is a system domain and is read-only. "
    f"The global knowledge base is read-only for agents; "
    f"use scope=workspace for writable notes."
)


@pytest.fixture
def global_kb(tmp_path, monkeypatch):
    """Build a temp global KB root and point the tool's root seams at it.

    Returns the root; the real user vault is never touched.
    """
    root = tmp_path / "global"
    (root / "system").mkdir(parents=True)
    (root / "user").mkdir(parents=True)
    (root / "system" / f"{DOMAIN}.md").write_text(SYSTEM_CONTENT, encoding="utf-8")

    # Write modes (append/update/create_domain) resolve via this helper ...
    monkeypatch.setattr(kb_module, "_get_global_kb_root", lambda: root)
    # ... read modes import GLOBAL_KB_DIR at call time.
    import agent.knowledge.global_kb as global_kb_module

    monkeypatch.setattr(global_kb_module, "GLOBAL_KB_DIR", root, raising=False)

    # Hermeticity: the fixture root must live under the pytest tmp dir, never
    # under the real user vault.
    assert root.is_relative_to(tmp_path)
    assert not root.is_relative_to(Path.home() / ".thoughtmachine")
    return root


def _run(**kwargs) -> str:
    """Build and execute a global-scope KnowledgeBaseTool (same kwargs idiom as
    ``tests/test_knowledge_base_tool.py``)."""
    params = {
        "effective_permissions": {"filesystem": "write"},
        "domain": DOMAIN,
        "scope": "global",
    }
    params.update(kwargs)
    return KnowledgeBaseTool(**params).execute()


# ---------------------------------------------------------------------------
# 1. update on a system domain is refused, with no side effects.
# ---------------------------------------------------------------------------


def test_update_on_system_domain_is_refused(global_kb):
    path = global_kb / "system" / f"{DOMAIN}.md"
    before = path.read_bytes()

    result = _run(mode="update", section="Rules", new_content="POISON")

    assert SYSTEM_REFUSAL in result, f"system-domain wording changed: {result}"
    assert "system domain and is read-only" in result
    assert path.read_bytes() == before, "system domain file must be byte-identical"
    assert MARKER in path.read_text(encoding="utf-8")
    assert "POISON" not in path.read_text(encoding="utf-8")
    assert not (global_kb / "user" / f"{DOMAIN}.md").exists(), (
        "a refused update must not create a user/ shadow of a system domain"
    )


# ---------------------------------------------------------------------------
# 2. append on a system domain is refused, with no side effects.
# ---------------------------------------------------------------------------


def test_append_on_system_domain_is_refused(global_kb):
    path = global_kb / "system" / f"{DOMAIN}.md"
    before = path.read_bytes()

    result = _run(mode="append", entry="POISON APPEND", summary="poison")

    assert SYSTEM_REFUSAL in result, f"system-domain wording changed: {result}"
    assert "system domain and is read-only" in result
    assert path.read_bytes() == before, "system domain file must be byte-identical"
    assert "POISON APPEND" not in path.read_text(encoding="utf-8")
    assert not (global_kb / "user" / f"{DOMAIN}.md").exists(), (
        "a refused append must not create a user/ shadow of a system domain"
    )


# ---------------------------------------------------------------------------
# 3. read on a system domain returns content (no stray for_write=True).
# ---------------------------------------------------------------------------


def test_read_on_system_domain_returns_content(global_kb):
    result = _run(mode="read")

    assert MARKER in result, "read must return the system domain's content"
    assert "system rules body" in result
    assert "read-only" not in result, "read must not hit the write-target refusal"


# ---------------------------------------------------------------------------
# 4. global write targets reject path-bearing names.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", ["a/b", "a\\b", "..", "../evil"])
def test_global_write_target_rejects_path_names(global_kb, bad_name):
    tool = KnowledgeBaseTool(
        mode="update",
        domain=DOMAIN,
        section="Rules",
        new_content="POISON",
        scope="global",
    )

    with pytest.raises(ValueError):
        tool._resolve_domain_path(global_kb, bad_name, for_write=True)

    # Nothing was created anywhere outside the fixture root, and the system
    # domain is untouched.
    markdown_files = sorted(
        p.relative_to(global_kb).as_posix() for p in global_kb.rglob("*.md")
    )
    assert markdown_files == [f"system/{DOMAIN}.md"]
    assert sorted(p.name for p in global_kb.parent.iterdir()) == ["global"]


# ---------------------------------------------------------------------------
# 5. global list output advertises reads only.
# ---------------------------------------------------------------------------


def test_global_user_domain_write_refused(global_kb):
    """The global KB is read-only for agents: writes to ``user/`` are refused.

    A ``filesystem: write`` grant is not enough to make the global KB writable:
    ``user/`` is reference material shipped with the product, not an
    agent-writable area, so every write mode must be refused and the target
    file must stay byte-identical (and no new domain may appear).
    """
    user_path = global_kb / "user" / "notes.md"
    user_path.write_text("# Notes\n\nUSER-MARKER-7\n", encoding="utf-8")
    before = user_path.read_bytes()

    results = {
        "update": _run(mode="update", domain="notes", section="Notes", new_content="POISON"),
        "append": _run(mode="append", domain="notes", entry="POISON APPEND", summary="poison"),
        "create_domain": _run(mode="create_domain", domain="fresh_domain", category="user"),
    }

    for mode, result in results.items():
        assert "read-only for agents" in result, (
            f"{mode} on a user/ domain must be refused as read-only: {result}"
        )
        assert "`user/`" in result, (
            f"{mode} on a user/ domain must name the `user/` area as the reason: {result}"
        )
        assert "is a system domain and is read-only" not in result, (
            f"{mode} on a user/ domain must not use the system-domain wording: {result}"
        )

    assert user_path.read_bytes() == before, "a refused write must leave user/ byte-identical"
    assert "POISON" not in user_path.read_text(encoding="utf-8")
    assert sorted(p.name for p in (global_kb / "user").iterdir()) == ["notes.md"], (
        "a refused write must not create a user/ domain in the global KB"
    )


def test_global_list_output_does_not_advertise_writes(global_kb):
    result = _run(mode="list")

    assert "~/.thoughtmachine/global/" in result
    assert "~/.thoughtmachine/knowledge/" not in result
    for write_hint in ("mode=append", "mode=update", "mode=create_domain"):
        assert write_hint not in result, f"global list must not advertise {write_hint}"
    for read_hint in (
        "mode=read domain=<name> scope=global",
        "mode=search query=<term> scope=global",
        "mode=status scope=global",
    ):
        assert read_hint in result
