"""Mutation guard for scripts/verify_doc_citations.py.

That checker is the only mechanical proof that every file citation in the spec
and the ledger points at a file that exists and a line that exists. A checker
that fails falsely gets switched off or rubber-stamped, so each rule it
implements needs a test that goes red when the rule is broken -- above all the
rules that are there because the naive implementation got them wrong:

  * a SHORT ``:123`` inherits a file named BEFORE it on its own line, else a file
    named earlier in its heading section. Never a file its own line merely
    mentions afterwards, and never a file an earlier TABLE ROW named (that
    mis-binding reported a ``:4660`` against a 303-line file);
  * a path is a citation only INSIDE an inline code span, so a negative mention
    in running prose is not a citation at all;
  * a code span may straddle a line break, so backtick parity is document-level,
    not per line.

Fixtures live under ``tmp_path`` and the checker is pointed at that directory as
its ``root``: the real documents change constantly and a test that asserted
their contents would rot instead of guarding the parser.

Dependency-free beyond pytest itself: no docker, no network, no vault.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER_PATH = REPO_ROOT / "scripts" / "verify_doc_citations.py"


def _load_checker():
    """Import scripts/verify_doc_citations.py by path ('scripts' is no package)."""
    spec = importlib.util.spec_from_file_location("verify_doc_citations_under_test", CHECKER_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {CHECKER_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


CHECKER = _load_checker()

#: The whole "repo" every case is checked against: a.py has 5 lines, sub/b.py 3.
FILES = {
    "a.py": "a1\na2\na3\na4\na5\n",
    "sub/b.py": "b1\nb2\nb3\n",
}


def check(tmp_path: Path, doc_text: str):
    """Check ``doc_text`` against a fresh fixture tree and return the DocResult."""
    for name, body in FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    doc = tmp_path / "docs" / "spec.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(doc_text, encoding="utf-8")
    return CHECKER.check_doc(doc, tmp_path)


def shorts(result) -> list:
    return [c for c in result.cites if c.kind == "SHORT"]


# --- MUST FAIL ---------------------------------------------------------------


def test_a_full_citation_of_a_missing_file_fails(tmp_path):
    result = check(tmp_path, "# Sec\n\nsee `ghost.py:3` for the detail\n")
    assert len(result.missing) == 1, [c.raw for c in result.missing]
    assert result.missing[0].raw == "ghost.py:3"
    assert not result.ok


def test_a_line_number_beyond_the_file_fails(tmp_path):
    # the off-by-one class: a.py has 5 lines, so :6 is a broken citation
    result = check(tmp_path, "# Sec\n\nsee `a.py:6` for the detail\n")
    assert len(result.out_of_bounds) == 1, [c.raw for c in result.out_of_bounds]
    assert result.out_of_bounds[0].raw == "a.py:6"
    assert result.out_of_bounds[0].n_lines == 5
    assert not result.ok


def test_a_short_with_no_file_in_its_section_is_unresolved(tmp_path):
    result = check(tmp_path, "# Sec\n\nsee `:2` for the detail\n")
    assert len(result.unresolved) == 1, [c.raw for c in result.unresolved]
    assert result.unresolved[0].raw == ":2"
    assert not result.ok


def test_a_table_row_does_not_inherit_from_an_earlier_row(tmp_path):
    doc = (
        "# Sec\n\n"
        "| item | where |\n"
        "| --- | --- |\n"
        "| one | `a.py:2` |\n"
        "| two | `:2` |\n"
    )
    result = check(tmp_path, doc)
    assert len(result.unresolved) == 1, [c.raw for c in result.failures]
    assert result.unresolved[0].raw == ":2"
    assert result.unresolved[0].target == ""


def test_a_prose_path_without_backticks_is_not_a_citation(tmp_path):
    # docs/CONTRACTS.md fixes the convention: a non-existent path is written
    # WITHOUT backticks, so a negative mention must not become a citation.
    result = check(tmp_path, "# Sec\n\nwe deleted missing_file.py from the tree\n")
    assert result.cites == [], [(c.kind, c.raw) for c in result.cites]
    assert not result.failures
    assert [c.raw for c in result.bare_outside_code] == ["missing_file.py"]


# --- MUST PASS ---------------------------------------------------------------


def test_a_short_inherits_the_file_named_earlier_in_the_section(tmp_path):
    doc = "# Sec\n\nnames `a.py:1` here.\n\nlater, `:2` in a plain paragraph.\n"
    result = check(tmp_path, doc)
    assert not result.failures, [(c.raw, c.note) for c in result.failures]
    assert [c.target for c in shorts(result)] == ["a.py"]
    assert shorts(result)[0].in_bounds


def test_a_short_binds_to_a_file_named_earlier_on_its_own_line(tmp_path):
    doc = "# Sec\n\n- Files: `sub/b.py` (new helper beside `:2`)\n"
    result = check(tmp_path, doc)
    assert not result.failures, [(c.raw, c.note) for c in result.failures]
    assert [c.target for c in shorts(result)] == ["sub/b.py"]
    assert shorts(result)[0].in_bounds


def test_a_runtime_artefact_is_excluded_not_missing(tmp_path):
    result = check(tmp_path, "# Sec\n\nThe worker writes `heartbeat.json` every second.\n")
    assert not result.failures, [(c.raw, c.note) for c in result.failures]
    assert [c.raw for c in result.excluded] == ["heartbeat.json"]


def test_a_code_span_may_straddle_a_line_break(tmp_path):
    # The opening `` run is on line 3 and the closing run on line 5; the path on
    # line 4 therefore carries NO backtick of its own. Per-line parity would call
    # it prose and drop the citation -- document-level parity keeps it.
    doc = (
        "# Sec\n\n"
        "The captured evidence reads ``\n"
        "sub/b.py is not there\n"
        "`` and that is the end of it.\n"
    )
    result = check(tmp_path, doc)
    assert not result.failures, [(c.raw, c.note) for c in result.failures]
    assert [c.raw for c in result.cites] == ["sub/b.py"]
    assert result.cites[0].resolved == "sub/b.py"
