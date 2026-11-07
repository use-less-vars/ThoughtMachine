"""Regression tests for :class:`tools.knowledge_base.KnowledgeBaseTool` update mode.

Covers ``bug/knowledgebase-tool-update-duplicates-section``:

* ``update`` must locate sections using the *same* semantics as ``read``
  (case-insensitive heading match), so any section ``read`` can resolve,
  ``update`` can also replace in place.
* When a section genuinely cannot be found, ``update`` must **not** write
  anything (previously it appended a duplicate ``## <section>`` block and
  corrupted the file). The file must be left byte-identical.
"""

from pathlib import Path

from tools.knowledge_base import KnowledgeBaseTool

DOMAIN = "test_notes"
REL_PATH = "personal/test_notes.md"

BASE_CONTENT = (
    "# Notes\n"
    "\n"
    "Intro text.\n"
    "\n"
    "## Alpha\n"
    "\n"
    "alpha body\n"
    "\n"
    "## Beta\n"
    "\n"
    "beta body\n"
    "\n"
    "## Gamma\n"
    "\n"
    "gamma body\n"
)


def _kb_root(tmp_path: Path) -> Path:
    return tmp_path / ".thoughtmachine" / "knowledge"


def _write_domain(
    tmp_path: Path, content: str = BASE_CONTENT, rel_path: str = REL_PATH
) -> Path:
    path = _kb_root(tmp_path) / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _run(tmp_path: Path, **kwargs) -> str:
    """Build and execute a workspace-scope KnowledgeBaseTool."""
    params = {
        "workspace_path": str(tmp_path),
        "effective_permissions": {"filesystem": "write"},
        "domain": DOMAIN,
        "scope": "workspace",
    }
    params.update(kwargs)
    return KnowledgeBaseTool(**params).execute()


# ---------------------------------------------------------------------------
# 1. Replace-in-place keeps exactly one copy of the heading.
# ---------------------------------------------------------------------------


def test_update_replaces_existing_section_in_place(tmp_path):
    path = _write_domain(tmp_path)

    result = _run(tmp_path, mode="update", section="Beta", new_content="beta REPLACED")

    content = path.read_text(encoding="utf-8")
    assert "✅" in result
    assert content.count("## Beta") == 1
    assert "beta REPLACED" in content
    assert "beta body" not in content
    # Sibling sections survive untouched.
    assert "## Alpha" in content and "alpha body" in content
    assert "## Gamma" in content and "gamma body" in content


def test_read_after_update_shows_new_content(tmp_path):
    path = _write_domain(tmp_path)

    _run(tmp_path, mode="update", section="Beta", new_content="beta REPLACED")

    read_back = _run(tmp_path, mode="read", section="Beta")
    assert "beta REPLACED" in read_back
    assert "beta body" not in read_back
    # The update never duplicated the heading.
    assert path.read_text(encoding="utf-8").count("## Beta") == 1


# ---------------------------------------------------------------------------
# 2. Missing section => no write, no append, file byte-identical.
# ---------------------------------------------------------------------------


def test_update_missing_section_makes_no_change(tmp_path):
    path = _write_domain(tmp_path)
    before = path.read_text(encoding="utf-8")

    result = _run(
        tmp_path, mode="update", section="Nonexistent", new_content="MUST NOT APPEAR"
    )

    after = path.read_text(encoding="utf-8")
    assert after == before, "file must be byte-identical when the section is not found"
    assert "## Nonexistent" not in after
    assert "MUST NOT APPEAR" not in after
    assert "not found" in result
    assert "No changes were made" in result


# ---------------------------------------------------------------------------
# 3. Case difference => update matches (like read) instead of duplicating.
# ---------------------------------------------------------------------------


def test_update_matches_section_case_insensitively_like_read(tmp_path):
    path = _write_domain(tmp_path)

    # read() resolves a differently-cased name...
    read_back = _run(tmp_path, mode="read", section="beta")
    assert "beta body" in read_back

    # ...therefore update() must resolve it too, without duplicating the heading.
    _run(tmp_path, mode="update", section="beta", new_content="beta REPLACED")

    content = path.read_text(encoding="utf-8")
    assert content.lower().count("## beta") == 1
    assert "beta REPLACED" in content
    assert "beta body" not in content


# ---------------------------------------------------------------------------
# 4. End-to-end regression: read-finds => update-finds, single copy preserved.
# ---------------------------------------------------------------------------


def test_update_read_roundtrip_preserves_single_copy(tmp_path):
    path = _write_domain(tmp_path)

    # Two successive updates whose names differ only in case.
    _run(tmp_path, mode="update", section="ALPHA", new_content="alpha v2")
    _run(tmp_path, mode="update", section="alpha", new_content="alpha v3")

    content = path.read_text(encoding="utf-8")
    assert content.lower().count("## alpha") == 1
    assert "alpha v3" in content
    assert "alpha v2" not in content
    assert "alpha body" not in content
