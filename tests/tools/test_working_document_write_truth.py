"""Truth tests for ``WorkingDocument`` (``tools/workspace/working_document.py``).

``_handle_create`` minted its own ``doc_id`` at ``:138`` (discarding the one the
caller passed) and built its section map from ``""`` literals at ``:144``
(discarding ``content``), so a ``create`` that named either could not be found
under that name afterwards. ``read``'s table of contents then rendered each
section as ``len(value.splitlines())`` — a bare integer where a body belongs.

These tests pin the contract the fix implements, all through ``execute()``:

* a caller-supplied ``doc_id`` is the id the document is readable under, and a
  colliding id is REFUSED naming the existing document (never re-minted);
* ``content`` at create time is what ``read`` returns;
* appended content is what ``read`` returns;
* ``content`` against anything but exactly one named section is REFUSED, and
  the refusal names the ambiguity;
* a legacy non-string section value is reported as unreadable WITH its raw
  value visible — never coerced to text, never hidden;
* the table of contents carries text, not integers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workspace.working_document import WorkingDocument


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


def _run(**kwargs) -> str:
    return WorkingDocument(**kwargs).execute()


def _write_handmade_doc(docs_dir: Path, doc_id: str, sections: dict) -> Path:
    """A document written by hand — e.g. one left behind by the old bug."""
    path = docs_dir / f"{doc_id}.json"
    path.write_text(
        json.dumps(
            {
                "doc_id": doc_id,
                "title": "Handmade",
                "sections": sections,
                "created_at": "2024-01-01T00:00:00",
                "updated_at": "2024-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    return path


# ── (a) the caller's doc_id is the document's id ─────────────────────────────


def test_create_uses_the_caller_supplied_doc_id(workspace, docs_dir):
    created = json.loads(
        _run(action="create", title="Plan", doc_id="wd-probe-55", sections=["notes"])
    )

    assert created["doc_id"] == "wd-probe-55"
    assert Path(created["path"]) == (docs_dir / "wd-probe-55.json").resolve()
    # The id it reported is the id the document is readable under.
    assert (docs_dir / "wd-probe-55.json").exists()
    toc = json.loads(_run(action="read", doc_id="wd-probe-55"))
    assert toc["title"] == "Plan"


def test_create_with_a_taken_doc_id_is_refused_naming_the_existing_document(workspace, docs_dir):
    created = json.loads(
        _run(action="create", title="First", doc_id="taken", sections=["notes"])
    )
    assert created["doc_id"] == "taken"
    before = (docs_dir / "taken.json").read_text(encoding="utf-8")

    refused = _run(action="create", title="Second", doc_id="taken", sections=["notes"])

    assert "Error" in refused, refused
    # It names the existing document ...
    assert "taken" in refused and "First" in refused, refused
    # ... the existing document is untouched ...
    assert (docs_dir / "taken.json").read_text(encoding="utf-8") == before
    # ... and no document was minted under a different id either.
    assert sorted(p.name for p in docs_dir.glob("*.json")) == ["taken.json"]


# ── (b) create-time content is stored and read back ─────────────────────────


def test_create_content_is_what_read_returns(workspace, docs_dir):
    created = json.loads(
        _run(
            action="create",
            title="Plan",
            sections=["notes"],
            content="probe-content-55-must-survive",
        )
    )

    assert (
        _run(action="read", doc_id=created["doc_id"], section="notes")
        == "probe-content-55-must-survive"
    )
    stored = json.loads(
        (docs_dir / f"{created['doc_id']}.json").read_text(encoding="utf-8")
    )
    assert stored["sections"]["notes"] == "probe-content-55-must-survive"


# ── (c) appended content is stored and read back ────────────────────────────


def test_appended_content_is_what_read_returns(workspace, docs_dir):
    created = json.loads(_run(action="create", title="Plan", sections=["notes"]))

    result = _run(
        action="append",
        doc_id=created["doc_id"],
        section="notes",
        content="appended-text-55",
    )

    assert "Appended to section 'notes'" in result, result
    body = _run(action="read", doc_id=created["doc_id"], section="notes")
    assert "appended-text-55" in body, body


def test_append_keeps_the_create_time_content(workspace, docs_dir):
    created = json.loads(
        _run(action="create", title="Plan", sections=["notes"], content="create-text-55")
    )

    _run(
        action="append",
        doc_id=created["doc_id"],
        section="notes",
        content="append-text-55",
    )

    body = _run(action="read", doc_id=created["doc_id"], section="notes")
    assert body.splitlines()[0] == "create-text-55", body
    assert "append-text-55" in body, body


# ── (d) content needs exactly one target section ────────────────────────────


def test_create_with_content_and_two_sections_is_refused(workspace, docs_dir):
    refused = _run(
        action="create",
        title="Plan",
        sections=["notes", "todo"],
        content="orphan-text",
    )

    assert "Error" in refused, refused
    assert "2" in refused, refused
    assert "exactly one" in refused.lower(), refused
    assert "content" in refused.lower(), refused
    # Nothing was written under any id.
    assert list(docs_dir.glob("*.json")) == []


def test_create_with_content_and_no_section_is_refused(workspace, docs_dir):
    refused = _run(action="create", title="Plan", content="orphan-text")

    assert "Error" in refused, refused
    assert "0" in refused or "none" in refused.lower(), refused
    assert "exactly one" in refused.lower(), refused
    assert list(docs_dir.glob("*.json")) == []


# ── read: text is text, and an unreadable value stays visible ───────────────


def test_toc_renders_text_not_bare_integers(workspace, docs_dir):
    created = json.loads(
        _run(
            action="create",
            title="Plan",
            sections=["notes"],
            content="first line\nsecond line",
        )
    )

    toc = json.loads(_run(action="read", doc_id=created["doc_id"]))

    assert isinstance(toc["sections"]["notes"], str), toc
    assert "2 line" in toc["sections"]["notes"], toc


def test_legacy_integer_section_is_reported_unreadable_with_its_raw_value(workspace, docs_dir):
    _write_handmade_doc(docs_dir, "legacy", {"notes": 1})

    body = _run(action="read", doc_id="legacy", section="notes")

    assert "unreadable" in body.lower(), body
    assert "1" in body, body  # the raw value is visible ...
    assert body != "1"  # ... and is not passed off as the section's text

    toc = json.loads(_run(action="read", doc_id="legacy"))
    assert isinstance(toc["sections"]["notes"], str), toc
    assert "1" in toc["sections"]["notes"], toc
