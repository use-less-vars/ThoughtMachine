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
    not per line;
  * a SYMBOL ``path.py::name`` is checked against that file's AST rather than
    against a line number, so refactoring above the definition cannot break it;
    an optional pinned ``:line`` must fall INSIDE the symbol, because the pin is
    there to be narrower than the symbol, and a SYMBOL naming a non-Python file
    is a failure rather than a skip (there is no AST to look the name up in).

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


#: Definitions to point SYMBOL citations at. Kept out of FILES so the line-number
#: fixtures above keep their exact shapes (a.py is deliberately 5 bare lines).
#: Line numbers matter here: helper 8-9, Widget 12-19, Widget.render 15-16,
#: Widget.Inner 18-19, CONST 4-4, PAIR_A/PAIR_B 5-5, _private 22-23.
SYMBOL_FILES = {
    "mod.py": (
        '"""Fixture module for SYMBOL citations."""\n'   # 1
        "import os\n"                                    # 2
        "\n"                                             # 3
        "CONST = 1\n"                                    # 4
        "PAIR_A, PAIR_B = 1, 2\n"                        # 5
        "\n"                                             # 6
        "\n"                                             # 7
        "def helper(a, b):\n"                            # 8
        "    return a + b\n"                             # 9
        "\n"                                             # 10
        "\n"                                             # 11
        "class Widget:\n"                                # 12
        '    """A widget."""\n'                          # 13
        "\n"                                             # 14
        "    def render(self):\n"                        # 15
        "        return helper(1, 2)\n"                  # 16
        "\n"                                             # 17
        "    class Inner:\n"                             # 18
        "        pass\n"                                 # 19
        "\n"                                             # 20
        "\n"                                             # 21
        "def _private():\n"                              # 22
        "    pass\n"                                     # 23
        "\n"                                             # 24
    ),
    "notes.txt": "plain text, no AST\n",
}


def _write_tree(tmp_path: Path, files: dict) -> None:
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _check(tmp_path: Path, files: dict, doc_text: str):
    _write_tree(tmp_path, files)
    doc = tmp_path / "docs" / "spec.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(doc_text, encoding="utf-8")
    return CHECKER.check_doc(doc, tmp_path)


def check(tmp_path: Path, doc_text: str):
    """Check ``doc_text`` against a fresh fixture tree and return the DocResult."""
    return _check(tmp_path, FILES, doc_text)


def check_symbols(tmp_path: Path, doc_text: str):
    """As ``check``, plus the Python fixture that SYMBOL citations resolve against."""
    return _check(tmp_path, {**FILES, **SYMBOL_FILES}, doc_text)


def shorts(result) -> list:
    return [c for c in result.cites if c.kind == "SHORT"]


def symbols(result) -> list:
    return [c for c in result.cites if c.kind == "SYMBOL"]


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


def test_a_symbol_the_file_does_not_define_fails(tmp_path):
    # the point of the kind: the name is looked up in the AST, so a name that is
    # not there is a failure even though the file exists and the lines are fine.
    result = check_symbols(tmp_path, "# Sec\n\nsee `mod.py::missing_symbol` for this\n")
    assert [c.raw for c in result.symbol_missing] == ["mod.py::missing_symbol"]
    assert not result.out_of_bounds, [c.raw for c in result.out_of_bounds]
    assert not result.ok


def test_a_symbol_on_a_non_python_file_fails_loudly(tmp_path):
    # there is no AST to look the name up in, so this must fail rather than skip
    result = check_symbols(tmp_path, "# Sec\n\nsee `notes.txt::CONST` for this\n")
    assert [c.raw for c in result.symbol_missing] == ["notes.txt::CONST"]
    assert "non-Python" in result.symbol_missing[0].note
    assert not result.ok


def test_a_pinned_line_outside_the_symbol_fails(tmp_path):
    # helper spans 8-9, so a pin at 1 is inside the FILE but outside the symbol:
    # the pin is there to be narrower than the symbol, never wider than it.
    result = check_symbols(tmp_path, "# Sec\n\nsee `mod.py::helper:1` for this\n")
    assert [c.raw for c in result.out_of_bounds] == ["mod.py::helper:1"]
    assert "outside symbol helper" in result.out_of_bounds[0].note
    assert not result.symbol_missing, [c.raw for c in result.symbol_missing]
    assert not result.ok


def test_a_missing_symbol_makes_the_cli_exit_one(tmp_path, monkeypatch, capsys):
    _write_tree(tmp_path, {**FILES, **SYMBOL_FILES})
    doc = tmp_path / "docs" / "spec.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Sec\n\nsee `mod.py::missing_symbol` for this\n", encoding="utf-8")
    real = CHECKER.check_doc
    # check_doc/main default to root=ROOT (the real repo), so point them at the
    # fixture tree; a switched-off checker would betray itself by exiting 0.
    monkeypatch.setattr(CHECKER, "check_doc", lambda d, root=None: real(d, tmp_path))
    assert CHECKER.main(["--doc", str(doc)]) == 1
    out = capsys.readouterr().out
    assert "symbols not defined: 1" in out
    assert "RESULT: FAIL" in out


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


def test_a_symbol_citation_of_a_module_function_passes(tmp_path):
    result = check_symbols(tmp_path, "# Sec\n\nsee `mod.py::helper` for this\n")
    assert not result.failures, [(c.raw, c.note) for c in result.failures]
    assert [(c.symbol, c.lines) for c in symbols(result)] == [("helper", (8, 9))]
    assert symbols(result)[0].resolved == "mod.py"


def test_a_dotted_symbol_citation_of_a_method_passes(tmp_path):
    doc = "# Sec\n\nsee `mod.py::Widget.render` and `mod.py::Widget.Inner` here\n"
    result = check_symbols(tmp_path, doc)
    assert not result.failures, [(c.raw, c.note) for c in result.failures]
    assert [(c.symbol, c.lines) for c in symbols(result)] == [
        ("Widget.render", (15, 16)),
        ("Widget.Inner", (18, 19)),
    ]


def test_a_symbol_citation_of_a_module_constant_passes(tmp_path):
    # an Assign target is not a def, and this is what makes a constant citable
    result = check_symbols(tmp_path, "# Sec\n\nsee `mod.py::CONST` for this\n")
    assert not result.failures, [(c.raw, c.note) for c in result.failures]
    assert [(c.symbol, c.lines) for c in symbols(result)] == [("CONST", (4, 4))]


def test_a_pinned_line_inside_the_symbol_passes(tmp_path):
    # Widget spans 12-19; the pin keeps its own line instead of taking the span
    result = check_symbols(tmp_path, "# Sec\n\nsee `mod.py::Widget:13` for this\n")
    assert not result.failures, [(c.raw, c.note) for c in result.failures]
    assert [(c.symbol, c.lines) for c in symbols(result)] == [("Widget", (13,))]


def test_the_real_ledger_cites_source_by_symbol():
    # A fixture can only prove the parser; this proves the shipped document uses
    # the kind at all, so the SYMBOL path cannot rot into dead code unnoticed.
    result = CHECKER.check_doc(REPO_ROOT / "docs" / "CONTRACTS.md", REPO_ROOT)
    assert len(result.cites) > 0
    found = symbols(result)
    assert found, "docs/CONTRACTS.md is supposed to cite source by symbol"
    broken = [(c.raw, c.note) for c in found if not (c.exists and c.in_bounds)]
    assert not broken, broken
