"""Regression tests for the workspace KB root and for `append` entry shaping.

Two defects are pinned here:

1. **Wrong checkout.** The workspace KB root was always derived from the
   session's *registered* workspace root, so a caller working in a linked
   git worktree of that workspace wrote (and then read) the KB inside the main
   checkout instead of its own worktree. The root must follow the checkout the
   caller is actually in -- and reads must agree with writes.

2. **Double heading.** `append` unconditionally prepended
   ``## YYYY-MM-DD -- <summary>`` to the entry, so an entry that already
   carried its own ``## `` heading was written with two headings (an
   unparseable block for `archive`).

The fake repositories below use only the two filesystem shapes git produces
(``.git`` directory for a main checkout, ``.git`` file holding a
``gitdir: <main>/.git/worktrees/<name>`` pointer for a linked worktree), so no
``git`` binary is required.
"""

from datetime import datetime
from pathlib import Path

from tools.knowledge_base import KnowledgeBaseTool

DOMAIN = "bugs_and_fixes"
REL_PATH = "personal/bugs_and_fixes.md"


# ---------------------------------------------------------------------------
# Fake checkouts
# ---------------------------------------------------------------------------


def _make_main_checkout(root: Path) -> Path:
    """Create ``root/repo`` with a ``.git`` DIRECTORY (a main checkout)."""
    main = root / "repo"
    (main / ".git").mkdir(parents=True)
    return main


def _make_worktree(main: Path, name: str = "wt", *, inside: bool = False) -> Path:
    """Create a linked worktree of *main* with a ``.git`` FILE pointer."""
    wt = (main / ".worktrees" / name) if inside else (main.parent / name)
    (main / ".git" / "worktrees" / name).mkdir(parents=True, exist_ok=True)
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text(
        f"gitdir: {main}/.git/worktrees/{name}\n", encoding="utf-8"
    )
    return wt


def _kb(workspace: Path) -> Path:
    return workspace / ".thoughtmachine" / "knowledge"


def _run(workspace_path, **kwargs) -> str:
    """Build and execute a workspace-scope KnowledgeBaseTool."""
    params = {
        "workspace_path": str(workspace_path),
        "effective_permissions": {"filesystem": "write"},
        "domain": DOMAIN,
        "scope": "workspace",
    }
    params.update(kwargs)
    return KnowledgeBaseTool(**params).execute()


# ---------------------------------------------------------------------------
# 1. A caller inside a linked worktree keeps its KB in that worktree.
# ---------------------------------------------------------------------------


def test_append_from_linked_worktree_stays_in_that_worktree(tmp_path, monkeypatch):
    main = _make_main_checkout(tmp_path)
    wt = _make_worktree(main)
    monkeypatch.chdir(wt)

    result = _run(main, mode="append", entry="note from the worktree")
    assert DOMAIN in result

    wt_file = _kb(wt) / REL_PATH
    assert wt_file.exists()
    assert "note from the worktree" in wt_file.read_text(encoding="utf-8")
    # The main checkout is never touched.
    assert not (main / ".thoughtmachine").exists()


def test_read_from_linked_worktree_agrees_with_append(tmp_path, monkeypatch):
    main = _make_main_checkout(tmp_path)
    wt = _make_worktree(main)
    monkeypatch.chdir(wt)

    _run(main, mode="append", entry="note visible from the worktree")

    read = _run(main, mode="read")
    assert "note visible from the worktree" in read

    # ... and the main checkout does not see it.
    monkeypatch.chdir(main)
    read_main = _run(main, mode="read")
    assert "note visible from the worktree" not in read_main
    assert "File not found" in read_main


def test_subdirectory_of_the_worktree_also_resolves_to_the_worktree(tmp_path, monkeypatch):
    main = _make_main_checkout(tmp_path)
    wt = _make_worktree(main)
    sub = wt / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    _run(main, mode="append", entry="deep note")
    assert "deep note" in (_kb(wt) / REL_PATH).read_text(encoding="utf-8")
    assert not (main / ".thoughtmachine").exists()


# ---------------------------------------------------------------------------
# 2. Non-worktree callers keep landing in the registered workspace root.
# ---------------------------------------------------------------------------


def test_caller_in_the_main_checkout_uses_the_main_checkout(tmp_path, monkeypatch):
    main = _make_main_checkout(tmp_path)
    wt = _make_worktree(main)
    monkeypatch.chdir(main)

    _run(main, mode="append", entry="note from main")
    assert "note from main" in (_kb(main) / REL_PATH).read_text(encoding="utf-8")
    assert not (wt / ".thoughtmachine").exists()


def test_caller_outside_any_checkout_uses_the_registered_root(tmp_path, monkeypatch):
    main = _make_main_checkout(tmp_path)
    wt = _make_worktree(main)
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.chdir(outside)

    _run(main, mode="append", entry="note from outside")
    assert "note from outside" in (_kb(main) / REL_PATH).read_text(encoding="utf-8")
    assert not (wt / ".thoughtmachine").exists()
    assert not (outside / ".thoughtmachine").exists()


def test_unrelated_repository_cannot_capture_the_kb(tmp_path, monkeypatch):
    main = _make_main_checkout(tmp_path)
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    monkeypatch.chdir(other)

    _run(main, mode="append", entry="note despite other repo")
    assert "note despite other repo" in (_kb(main) / REL_PATH).read_text(encoding="utf-8")
    assert not (other / ".thoughtmachine").exists()


def test_registered_root_outside_a_checkout_is_not_retargeted(tmp_path, monkeypatch):
    """A checkout in the caller's ancestry never overrides a non-repo workspace."""
    main = _make_main_checkout(tmp_path)
    legacy = tmp_path / "legacy_ws"
    legacy.mkdir()
    monkeypatch.chdir(main)

    _run(legacy, mode="append", entry="legacy note")
    assert "legacy note" in (_kb(legacy) / REL_PATH).read_text(encoding="utf-8")
    assert not (main / ".thoughtmachine").exists()


def test_explicit_worktree_root_wins_over_the_caller_cwd(tmp_path, monkeypatch):
    """A session bound to a worktree writes there even with a cwd elsewhere."""
    main = _make_main_checkout(tmp_path)
    wt = _make_worktree(main, "worker", inside=True)
    monkeypatch.chdir(main)

    result = _run(main, worktree_root=str(wt), mode="append", entry="worker note")
    assert DOMAIN in result
    assert "worker note" in (_kb(wt) / REL_PATH).read_text(encoding="utf-8")
    assert not (main / ".thoughtmachine").exists()


# ---------------------------------------------------------------------------
# 3. `append` must not synthesise a heading on top of an entry's own heading.
# ---------------------------------------------------------------------------


def test_append_entry_with_own_heading_is_not_double_headed(tmp_path):
    entry = "## 2024-01-02 — pre-shaped entry\n\nbody line"
    _run(tmp_path, mode="append", entry=entry)

    content = (_kb(tmp_path) / REL_PATH).read_text(encoding="utf-8")
    # Exactly one dated heading: the one the caller supplied.
    assert content.count("## 2024-01-02") == 1
    today = datetime.now().strftime("%Y-%m-%d")
    assert f"## {today}" not in content
    assert content.endswith("\n## 2024-01-02 — pre-shaped entry\n\nbody line\n")


def test_append_plain_entry_still_gets_the_dated_heading(tmp_path):
    _run(tmp_path, mode="append", entry="plain body", summary="plain summary")

    content = (_kb(tmp_path) / REL_PATH).read_text(encoding="utf-8")
    today = datetime.now().strftime("%Y-%m-%d")
    assert content.endswith(f"\n## {today} — plain summary\n\nplain body\n")


def test_append_entry_with_own_heading_is_archiveable(tmp_path):
    """The single, caller-supplied dated heading stays a valid archive block."""
    _run(
        tmp_path,
        mode="append",
        entry="## 2020-01-01 — ancient entry\n\nold body",
    )
    result = _run(tmp_path, mode="archive", older_than_days=30)

    assert "Archived 1 entry" in result
    file_path = _kb(tmp_path) / REL_PATH
    content = file_path.read_text(encoding="utf-8")
    assert "old body" not in content
    archived = list((_kb(tmp_path) / "_archive").rglob("*.md"))
    assert any("old body" in p.read_text(encoding="utf-8") for p in archived)
