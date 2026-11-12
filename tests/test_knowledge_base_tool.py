"""Regression tests for :class:`tools.knowledge_base.KnowledgeBaseTool` update mode.

Covers ``bug/knowledgebase-tool-update-duplicates-section``:

* ``update`` must locate sections using the *same* semantics as ``read``
  (case-insensitive heading match), so any section ``read`` can resolve,
  ``update`` can also replace in place.
* When a section genuinely cannot be found, ``update`` must **not** write
  anything (previously it appended a duplicate ``## <section>`` block and
  corrupted the file). The file must be left byte-identical.
"""

from datetime import datetime, timedelta
from pathlib import Path

import tools.knowledge_base as kb_module
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


# ---------------------------------------------------------------------------
# mode=archive — move dated entries older than older_than_days into _archive/
# ---------------------------------------------------------------------------

OLD_BODY = "old body: bug/alpha-shape fixed"
MID_BODY = "mid body: bug/beta-shape fixed"
NEW_BODY = "new body: still open"

DATED_HEADING_RE = r"^## \d{4}-\d{2}-\d{2} — "


def _day(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _entry(body: str, days_ago: int, summary: str = "note") -> str:
    """Reproduce the exact block ``mode=append`` writes."""
    return f"\n## {_day(days_ago)} — {summary}\n\n{body}\n"


def _archive_path(tmp_path: Path) -> Path:
    return _kb_root(tmp_path) / "_archive" / REL_PATH


def test_archive_moves_old_entries_and_keeps_recent(tmp_path):
    old = _entry(OLD_BODY, 400, "ancient")
    mid = _entry(MID_BODY, 10, "ten days")
    new = _entry(NEW_BODY, 1, "yesterday")
    content = "# Notes\n\nIntro.\n" + old + mid + new
    path = _write_domain(tmp_path, content)

    result = _run(tmp_path, mode="archive", domain=DOMAIN, older_than_days=30)

    live = path.read_text(encoding="utf-8")
    archived = _archive_path(tmp_path).read_text(encoding="utf-8")

    assert "✅ Archived 1 entry" in result
    assert "Entries archived: 1" in result
    assert "Headings without a parseable ISO date kept in place: 0" in result
    assert OLD_BODY not in live
    assert MID_BODY in live and NEW_BODY in live
    assert "Intro." in live
    # Archival is verbatim: the moved block is byte-identical to the original.
    assert old.strip("\n") in archived
    assert archived.count(OLD_BODY) == 1
    assert f"Bytes freed from the live file: {len(content) - len(live)}" in result
    assert "`_archive/personal/test_notes.md`" in result


def test_archive_keeps_undated_headings_and_reports_them(tmp_path):
    content = (
        "# Notes\n\nIntro.\n\n## Known Bugs\n\nundated body\n"
        + _entry(NEW_BODY, 2, "recent")
    )
    path = _write_domain(tmp_path, content)
    before = path.read_bytes()

    result = _run(tmp_path, mode="archive", domain=DOMAIN)

    assert "Nothing to archive" in result
    assert "Headings without a parseable ISO date kept in place: 1" in result
    assert "Dated entries kept: 1" in result
    assert path.read_bytes() == before, "nothing old enough => file byte-identical"
    assert not _archive_path(tmp_path).exists(), "no archive file may be created"


def test_archive_keeps_unparseable_date_headings(tmp_path):
    content = "# Notes\n\n## 2024-13-45 — bogus date\n\nbogus body\n"
    path = _write_domain(tmp_path, content)
    before = path.read_bytes()

    result = _run(tmp_path, mode="archive", domain=DOMAIN)

    assert "Nothing to archive" in result
    assert "Headings without a parseable ISO date kept in place: 1" in result
    assert path.read_bytes() == before
    assert not _archive_path(tmp_path).exists()


def test_archive_defaults_to_thirty_days(tmp_path):
    content = "# Notes\n\nIntro.\n" + _entry(MID_BODY, 10, "ten days")
    path = _write_domain(tmp_path, content)

    result = _run(tmp_path, mode="archive", domain=DOMAIN)

    assert "older than 30 days" in result
    assert "Nothing to archive" in result
    assert MID_BODY in path.read_text(encoding="utf-8")


def test_archive_older_than_days_controls_the_cutoff(tmp_path):
    content = "# Notes\n\nIntro.\n" + _entry(MID_BODY, 10, "ten days")
    path = _write_domain(tmp_path, content)

    result = _run(tmp_path, mode="archive", domain=DOMAIN, older_than_days=5)

    assert "Entries archived: 1" in result
    assert "older than 5 days" in result
    assert MID_BODY not in path.read_text(encoding="utf-8")
    assert MID_BODY in _archive_path(tmp_path).read_text(encoding="utf-8")


def test_archive_is_idempotent(tmp_path):
    content = "# Notes\n\nIntro.\n" + _entry(OLD_BODY, 400, "ancient")
    path = _write_domain(tmp_path, content)

    first = _run(tmp_path, mode="archive", domain=DOMAIN)
    live_after_first = path.read_bytes()
    archive_after_first = _archive_path(tmp_path).read_bytes()

    second = _run(tmp_path, mode="archive", domain=DOMAIN)

    assert "Entries archived: 1" in first
    assert "Nothing to archive" in second
    assert path.read_bytes() == live_after_first
    assert _archive_path(tmp_path).read_bytes() == archive_after_first
    assert archive_after_first.decode("utf-8").count(OLD_BODY) == 1


def test_archive_domain_file_absent_is_an_error(tmp_path):
    result = _run(tmp_path, mode="archive", domain="missing_domain")
    # Unknown domain resolves to the list of valid domains, never a write.
    assert "missing_domain" in result
    assert "✅" not in result


def test_archive_requires_filesystem_write(tmp_path):
    _write_domain(tmp_path)
    tool = KnowledgeBaseTool(
        workspace_path=str(tmp_path),
        effective_permissions={"filesystem": "read"},
        mode="archive",
        domain=DOMAIN,
    )

    result = tool.execute()

    assert "filesystem:write" in result
    assert "✅" not in result


def test_archive_is_declared_as_a_write_mode():
    assert KnowledgeBaseTool.get_required_categories({"mode": "archive"}) == [
        "filesystem:write"
    ]


def test_archive_global_scope_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "global"
    user_dir = root / "user"
    user_dir.mkdir(parents=True)
    notes = user_dir / "notes.md"
    notes.write_text(
        "# Notes\n\n## 2020-01-01 — ancient\n\nancient body\n", encoding="utf-8"
    )
    before = notes.read_bytes()

    import agent.knowledge.global_kb as global_kb_module

    monkeypatch.setattr(kb_module, "_get_global_kb_root", lambda: root)
    monkeypatch.setattr(global_kb_module, "GLOBAL_KB_DIR", root, raising=False)

    tool = KnowledgeBaseTool(
        workspace_path=str(tmp_path),
        effective_permissions={"filesystem": "write"},
        mode="archive",
        domain="notes",
        scope="global",
    )
    result = tool.execute()

    assert "read-only for agents" in result
    assert notes.read_bytes() == before, "a refused archive must not touch the file"
    assert not (root / "_archive").exists()

