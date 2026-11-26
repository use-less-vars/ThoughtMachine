"""Partial-line ``find`` strings in ``ApplyEdits`` (``tools/apply_edits.py``).

``_apply_single_edit`` locates ``find`` at a raw character offset and rebuilds
the file as ``content[:pos] + replace + content[pos + len(find):]``, so a find
string that was only part of a line replaced that span and left the rest of the
line glued onto the replacement text. The tool still reported
``Successfully applied 1 edit(s).`` — a line-oriented sentence about a character
splice — so the damaged line read as plausible prose.

These tests pin the contract the fix implements, all through ``execute()``:

* a ``find`` that matches only part of a line is REFUSED by default, and the
  refusal shows both the matched span and the unmatched remainder of the line;
* the file on disk is UNCHANGED after such a refusal;
* whole-line and whole-block edits still apply, and the result names the
  matched span per edit;
* the opt-in ``allow_partial_match`` flag (default off) splices the span and
  says the match was partial.
"""

from __future__ import annotations

import pytest

from tools.apply_edits import ApplyEdits

_LINE = (
    "**Consistency obligation:** every structural claim here must agree with "
    "table 7.3 and with the counts in appendix B; any disagreement must be corrected."
)
# A word-aligned PREFIX of _LINE, not the whole line.
_FIND = "**Consistency obligation:** every structural claim here must agree with table 7.3"
_REPLACE = (
    "**Consistency obligation:** every structural claim here must agree with table 7.4; "
    "any disagreement must be corrected."
)


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "d.md"
    path.write_text("intro line\n" + _LINE + "\ntrailing line\n", encoding="utf-8")
    return path


def _edit(path, edit, **kwargs) -> str:
    return ApplyEdits(
        file_path=str(path),
        edits=[edit],
        workspace_path=str(path.parent),
        **kwargs,
    ).execute()


# ── (a) a partial-line match is refused, loudly ─────────────────────────────


def test_partial_line_find_is_refused_with_the_span_and_the_remainder(doc):
    out = _edit(doc, {"find": _FIND, "replace": _REPLACE})

    assert "ERROR" in out, out
    assert "partial" in out.lower(), out
    # The matched span is shown ...
    assert "table 7.3" in out, out
    # ... and so is the unmatched remainder of that line.
    assert "appendix B" in out, out
    assert "any disagreement must be corrected." in out, out


def test_mid_token_find_is_refused(tmp_path):
    path = tmp_path / "code.py"
    path.write_text("concatenate = 1\n", encoding="utf-8")

    out = _edit(path, {"find": "cat", "replace": "DOG"})

    assert "ERROR" in out, out
    assert "cat" in out and "con" in out and "enate = 1" in out, out


# ── (b) the refusal leaves the file untouched ───────────────────────────────


def test_refusal_leaves_the_file_unchanged(doc):
    before = doc.read_text(encoding="utf-8")

    out = _edit(doc, {"find": _FIND, "replace": _REPLACE})

    assert "ERROR" in out, out
    assert doc.read_text(encoding="utf-8") == before


# ── (c) whole-line and whole-block finds keep working, and say what matched ──


def test_whole_line_edit_still_applies_and_names_the_matched_span(doc):
    out = _edit(doc, {"find": "intro line", "replace": "opening line"})

    assert "ERROR" not in out, out
    assert "Successfully applied 1 edit(s)." in out, out
    assert "whole line 1" in out, out
    assert "intro line" in out, out

    body = doc.read_text(encoding="utf-8")
    assert body.splitlines()[0] == "opening line"
    assert _LINE in body  # the neighbouring lines are untouched


def test_whole_block_edit_still_applies(doc):
    out = _edit(doc, {"find": "intro line\n" + _LINE, "replace": "replaced block"})

    assert "ERROR" not in out, out
    assert doc.read_text(encoding="utf-8") == "replaced block\ntrailing line\n"


# ── (d) the opt-in flag splices the span, and says so ───────────────────────


def test_partial_match_flag_applies_the_splice_and_reports_it_as_partial(doc):
    out = _edit(doc, {"find": _FIND, "replace": _REPLACE}, allow_partial_match=True)

    assert "ERROR" not in out, out
    assert "partial" in out.lower(), out

    body = doc.read_text(encoding="utf-8")
    assert "table 7.4" in body
    # The deliberate span splice: the rest of the line survives.
    assert "appendix B" in body


def test_partial_match_is_off_by_default(doc):
    """The flag exists and defaults off — no opt-in, no splice."""
    assert ApplyEdits.model_fields["allow_partial_match"].default is False

    out = _edit(doc, {"find": _FIND, "replace": _REPLACE})

    assert "ERROR" in out, out
    assert "table 7.4" not in doc.read_text(encoding="utf-8")


# ── (e) the regex path enforces the same contract ────────────────────────────

# A regex that lands mid-line, where a regex is likeliest to land.
_REGEX_FIND = r"table 7\.3"
_REGEX_REPLACE = "table 7.4"
# A regex that covers a whole line, metacharacter and all.
_WHOLE_LINE_REGEX_FIND = r"intro\s+line"


def test_regex_partial_line_match_is_refused_with_the_span_and_the_remainder(doc):
    before = doc.read_text(encoding="utf-8")

    out = _edit(
        doc, {"find": _REGEX_FIND, "replace": _REGEX_REPLACE}, use_regex=True
    )

    assert "ERROR" in out, out
    assert "partial" in out.lower(), out
    # The matched span is shown ...
    assert "table 7.3" in out, out
    # ... and so is the unmatched remainder on that line, before and after it.
    assert "structural claim" in out, out
    assert "appendix B" in out, out
    # The refusal is a refusal: the file on disk is untouched.
    assert doc.read_text(encoding="utf-8") == before
    assert "table 7.4" not in before


def test_regex_partial_match_flag_applies_the_splice_and_reports_it_as_partial(doc):
    out = _edit(
        doc,
        {"find": _REGEX_FIND, "replace": _REGEX_REPLACE},
        use_regex=True,
        allow_partial_match=True,
    )

    assert "ERROR" not in out, out
    assert "partial line" in out, out

    body = doc.read_text(encoding="utf-8")
    assert "table 7.4" in body
    # The deliberate span splice: the rest of the line survives.
    assert "appendix B" in body


def test_regex_whole_line_match_applies_without_the_flag(doc):
    out = _edit(
        doc, {"find": _WHOLE_LINE_REGEX_FIND, "replace": "opening line"}, use_regex=True
    )

    assert "ERROR" not in out, out
    assert "Successfully applied 1 edit(s)." in out, out
    assert "whole line 1" in out, out

    body = doc.read_text(encoding="utf-8")
    assert body.splitlines()[0] == "opening line"
    assert _LINE in body  # the neighbouring lines are untouched
