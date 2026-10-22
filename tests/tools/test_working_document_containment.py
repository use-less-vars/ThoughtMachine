"""Containment tests for ``WorkingDocument`` (``tools/workspace/working_document.py``).

``doc_id`` is caller-supplied and is interpolated into a file name, so before
the fix a document id such as ``../../secret`` resolved *outside*
``.thoughtmachine/working_docs/`` and ``read``/``append`` would open (and with
``append``, rewrite) any ``<doc_id>.json`` on disk — including files of a
completely different repository.

These tests pin the containment guarantee: every ``doc_id`` is resolved
strictly inside the working-docs directory, traversal and absolute ids are
refused, and the legitimate create/append/read/list round-trip is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workspace.working_document import WorkingDocument

# The exact id shape produced by ``_handle_create`` (uuid4().hex[:12]).
_HEX_ID = "0123456789ab"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point ``WorkingDocument`` at ``tmp_path`` as its resolved workspace."""
    monkeypatch.setattr(
        WorkingDocument, "_resolve_registry_workspace", lambda self: str(tmp_path)
    )
    return tmp_path


@pytest.fixture
def docs_dir(workspace):
    path = workspace / ".thoughtmachine" / "working_docs"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def outside_document(workspace):
    """A ``*.json`` document *outside* the working-docs directory."""
    path = workspace / "secret.json"
    path.write_text(
        json.dumps({"title": "SECRET", "sections": {"notes": "TOPSECRET"}}),
        encoding="utf-8",
    )
    return path


def _run(**kwargs) -> str:
    return WorkingDocument(**kwargs).execute()


def _write_valid_doc(docs_dir: Path, doc_id: str = _HEX_ID) -> Path:
    path = docs_dir / f"{doc_id}.json"
    path.write_text(
        json.dumps(
            {
                "doc_id": doc_id,
                "title": "Real doc",
                "sections": {"notes": "inside-only"},
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    return path


# ── Traversal refusal ──────────────────────────────────────────────────────


def test_read_refuses_parent_directory_traversal(docs_dir, outside_document):
    result = _run(action="read", doc_id="../secret")

    assert "Invalid doc_id" in result
    assert "TOPSECRET" not in result
    assert "SECRET" not in result


def test_read_refuses_deep_traversal(docs_dir, outside_document):
    # The reported exploit shape: climb out of the workspace entirely.
    result = _run(action="read", doc_id="../../../../PycharmProjects/x/package")

    assert "Invalid doc_id" in result
    assert "TOPSECRET" not in result


def test_read_refuses_absolute_doc_id(docs_dir, outside_document):
    result = _run(action="read", doc_id=str(outside_document.with_suffix("")))

    assert "Invalid doc_id" in result
    assert "TOPSECRET" not in result


def test_read_toc_refuses_parent_directory_traversal(docs_dir, outside_document):
    # Even the TOC path (no section argument) must not parse a foreign file.
    result = _run(action="read", doc_id="../secret")

    assert "Invalid doc_id" in result
    assert "SECRET" not in result


def test_append_refuses_parent_directory_traversal(docs_dir, outside_document):
    before = outside_document.read_text(encoding="utf-8")

    result = _run(
        action="append", doc_id="../secret", section="notes", content="INJECTED"
    )

    assert "Invalid doc_id" in result
    assert outside_document.read_text(encoding="utf-8") == before
    assert "INJECTED" not in before


def test_append_refuses_absolute_doc_id(docs_dir, outside_document):
    before = outside_document.read_text(encoding="utf-8")

    result = _run(
        action="append",
        doc_id=str(outside_document.with_suffix("")),
        section="notes",
        content="INJECTED",
    )

    assert "Invalid doc_id" in result
    assert outside_document.read_text(encoding="utf-8") == before


def test_missing_document_still_reports_not_found(docs_dir):
    result = _run(action="read", doc_id="deadbeefcafe")

    assert result == "Error: Document 'deadbeefcafe' not found."


# ── Legitimate behaviour preserved ─────────────────────────────────────────


def test_create_append_read_round_trip(workspace, docs_dir):
    created = json.loads(_run(action="create", title="Plan", sections=["notes", "todo"]))
    doc_id = created["doc_id"]

    # The document really lives inside the working-docs directory.
    assert Path(created["path"]) == (docs_dir / f"{doc_id}.json").resolve()
    assert Path(created["path"]).parent == docs_dir.resolve()

    appended = _run(action="append", doc_id=doc_id, section="notes", content="first line")
    assert appended == f"Appended to section 'notes' in document '{doc_id}'."

    body = _run(action="read", doc_id=doc_id, section="notes")
    assert "first line" in body

    toc = json.loads(_run(action="read", doc_id=doc_id))
    assert toc["title"] == "Plan"
    assert set(toc["sections"]) == {"notes", "todo"}
    assert toc["sections"]["notes"] == 1
    assert toc["sections"]["todo"] == 0


def test_append_to_missing_section_is_created(docs_dir):
    _write_valid_doc(docs_dir)

    result = _run(action="append", doc_id=_HEX_ID, section="extra", content="hello")

    assert "Appended to section 'extra'" in result
    reloaded = json.loads((docs_dir / f"{_HEX_ID}.json").read_text(encoding="utf-8"))
    assert "hello" in reloaded["sections"]["extra"]
    # the pre-existing section is untouched
    assert reloaded["sections"]["notes"] == "inside-only"


def test_read_returns_existing_section(docs_dir):
    _write_valid_doc(docs_dir)

    assert _run(action="read", doc_id=_HEX_ID, section="notes") == "inside-only"
    assert _run(action="read", doc_id=_HEX_ID, section="absent") == (
        f"Error: Section 'absent' not found in document '{_HEX_ID}'."
    )


def test_list_reports_created_document(docs_dir):
    _write_valid_doc(docs_dir)

    listed = json.loads(_run(action="list"))

    assert [entry["doc_id"] for entry in listed] == [_HEX_ID]
    assert listed[0]["title"] == "Real doc"


def test_list_ignores_files_outside_docs_dir(docs_dir, outside_document):
    _write_valid_doc(docs_dir)

    listed = json.loads(_run(action="list"))

    assert [entry["doc_id"] for entry in listed] == [_HEX_ID]
