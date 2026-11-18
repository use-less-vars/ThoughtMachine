#!/usr/bin/env python3
"""Mechanically verify every file citation in the given spec/doc files.

Extracted citation forms
------------------------
1. FULL   ``path/to/file.py:123`` and ``path/to/file.py:123-456``
2. SHORT  ``:123`` / ``:123-456`` -- a continuation reference that inherits the
          file path from a preceding FULL citation, or from a preceding BARE
          path that sits inside an inline code span. Three rules, in this order:

          (a) THE SAME LINE WINS, PRECEDING ONLY. If the SHORT's own line names
              a file -- a FULL citation, or a BARE path inside a code span --
              BEFORE the SHORT, the nearest such naming binds it. A file named
              AFTER the citation on the same line does NOT bind: a path trailing
              a number that cites several things is an ambiguous suffix, not an
              antecedent. A line such as ``- Files: `infra/container_manager.py`
              (new `sweep_...` beside `:5343`)`` is unambiguous to a reader, so
              it is unambiguous here.

              This is MEASURED, not a matter of taste. A nearest-AFTER fallback
              produced 7 bindings across the two documents this checker guards
              (docs/design-worker-lifecycle.md and docs/CONTRACTS.md): all 7
              failed verification, 0 resolved cleanly. docs/CONTRACTS.md:195 is
              the clearest case -- it writes ``value
              `_ceiling_fail_closed_value` `:272-278`) and pinned by
              `tests/test_ceiling_value_fail_closed.py```, where ``:272-278`` is
              the ``apply_workspace_ceiling`` range in
              ``security/security_gate.py`` (the file that section names at line
              91; 1916 lines; in bounds), yet nearest-after stole the citation
              to the 120-line test file the line merely MENTIONS at its end. The
              section is the authority for a number whose own line only mentions
              a file in passing.

          (b) THEN THE HEADING SECTION. Failing (a), the SHORT inherits the
              nearest preceding such reference in the current SECTION, which
              runs from an ATX heading (``#``..``######`` followed by a space)
              to the next heading. A blank line does NOT end a section: this
              spec names a file once and then cites ``:123`` for several
              paragraphs, and treating a blank line as a boundary made that
              legitimate style unverifiable. Inheritance never crosses a
              heading -- doing so binds a ``:123`` to a file the prose never
              named, which is exactly the false-out-of-bounds noise this checker
              exists to remove.

          (c) TABLE ROWS DO NOT INHERIT. When the SHORT sits in a Markdown table
              row (optional whitespace then ``|``), only rule (a) applies: a row
              that names no file itself is never bound to the file named by an
              earlier row. Rows in this spec routinely cite different files, and
              row-to-row inheritance silently mis-binds them (it reported a
              ``:4660`` against a 303-line file).

          A SHORT citation that none of the three rules binds stays in the
          ``unresolved`` category, which is a FAILURE like any other -- never
          dropped, never guessed.
3. BARE   a file path mentioned with no line number (``infra/container_manager.py``),
          counted as a citation ONLY when it sits inside an inline code span
          (document-level backtick parity, fence-aware: a span may legally
          straddle a line break, which is why parity is tracked across the whole
          document instead of per line). Delimiter runs pair only with a run of
          EQUAL length: docs/design-worker-lifecycle.md:38 contains unbalanced
          ```` ``stopped``` ```` markup, so a naive document-wide parity flips
          on that one row and then misreads the rest of its table. A bare path
          in running prose is NOT a
          citation: docs/CONTRACTS.md fixes the convention that a path mentioned
          as NON-EXISTENT is written WITHOUT backticks precisely so that a
          parser cannot mistake a negative mention for a citation, and this
          parser honours that convention instead of flagging the negative
          mention as a broken citation. A bare reference containing ``/`` is
          resolved as a worktree-relative path; a bare basename is resolved by
          basename lookup anywhere in the worktree (a spec may say
          ``worker_thread.py`` for ``tools/workspace/worker_thread.py``), and
          only fails when no file of that name exists at all.

Checks per citation: (a) the file exists, (b) every cited line number lies in
1..len(file lines). No sampling -- every match is emitted.

Per-worker runtime artefacts written under the vault worker dir (``command.json``,
``context.json``, ``status.json``, ``heartbeat.json``) are named in the prose but
are never repo files, so the existence check does not apply to them.

Usage:  python3 scripts/verify_doc_citations.py [-v] [--doc PATH]...
        ``--doc`` is repeatable and accepts both ``--doc PATH`` and
        ``--doc=PATH``; with no ``--doc`` the script checks
        ``docs/design-worker-lifecycle.md``. Every named document is reported
        separately and the exit code is 1 if ANY of them fails, 0 only when all
        of them are clean.
Exit code 0 when every citation resolves, 1 otherwise, 2 on a usage error.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "design-worker-lifecycle.md"

SKIP_DIRS = {".git", "node_modules", ".worktrees", "__pycache__", ".mypy_cache", ".pytest_cache", "venv", ".venv"}

# Per-worker runtime artefacts written under the vault worker dir -- named in the
# prose but never repo files, so a repo-existence check does not apply to them.
RUNTIME_ARTEFACTS = {"command.json", "context.json", "status.json", "heartbeat.json"}

# path.ext:LINE or path.ext:LINE-LINE
FULL_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,6}):(\d+)(?:-(\d+))?")
# bare :LINE / :LINE-LINE (not part of a FULL match -- spans are filtered later)
SHORT_RE = re.compile(r"(?<![\w./:]):(\d+)(?:-(\d+))?")
# bare file reference
BARE_RE = re.compile(
    r"(?<![\w./-])([A-Za-z0-9_][A-Za-z0-9_./-]*\."
    r"(?:py|pyi|md|json|jsonl|js|jsx|mjs|cjs|ts|tsx|yaml|yml|toml|sh|bat|ps1|txt|cfg|ini|html|css|spec))"
    r"(?![\w/:])"
)
# Inline code spans do not cross a blank line, an ATX heading or a fenced-code
# delimiter; an ATX heading also opens a new SHORT-inheritance section.
ATX_HEADING_RE = re.compile(r"^#{1,6}\s")
FENCE_RE = re.compile(r"^\s{0,3}```")
# A Markdown table row: optional whitespace then a pipe. Rows inherit only from
# their own line -- see rule (c) in the module docstring.
TABLE_ROW_RE = re.compile(r"^\s*\|")


@dataclass
class Citation:
    doc_line: int          # line number in the spec doc
    raw: str               # citation exactly as written
    kind: str              # FULL | SHORT | BARE
    target: str            # referenced path (as written / resolved for SHORT)
    lines: tuple = ()      # cited line numbers
    resolved: str = ""     # worktree-relative path actually used
    exists: bool = False
    n_matches: int = 0     # basename matches when resolved by basename
    n_lines: int = 0
    in_bounds: bool = True
    note: str = ""


@dataclass
class DocResult:
    """Everything one document's run produced, with the failures pre-bucketed."""

    doc: Path
    label: str                              # display name of the document
    n_lines: int
    doc_lines: list = field(default_factory=list)
    cites: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    out_of_bounds: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    bare_outside_code: list = field(default_factory=list)

    @property
    def failures(self) -> list:
        return self.missing + self.out_of_bounds + self.unresolved

    @property
    def clean(self) -> int:
        return len(self.cites) - len(self.failures) - len(self.excluded)

    @property
    def ok(self) -> bool:
        return not self.failures


_BASENAME_CACHE: dict = {}


def _basename_index(root: Path = ROOT) -> dict:
    cached = _BASENAME_CACHE.get(root)
    if cached is not None:
        return cached
    index: dict = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        index.setdefault(p.name, []).append(p)
    _BASENAME_CACHE[root] = index
    return index


def _ends_block(line: str) -> bool:
    """True for the lines an inline code span cannot cross."""
    return not line.strip() or bool(ATX_HEADING_RE.match(line)) or bool(FENCE_RE.match(line))


def _code_spans(doc_lines: list) -> list:
    """Per-line masks marking the characters that sit inside an inline code span.

    Document-level backtick parity: the state is carried across line breaks,
    because a legal code span may straddle them (a quoted multi-line
    ``NOT FOUND ...`` evidence block is one span, and per-line parity would
    misread its middle lines as prose). A run of backticks is ONE delimiter and
    pairs only with a run of the SAME length -- docs/design-worker-lifecycle.md:38
    contains unbalanced ```` ``stopped``` ```` markup, and naive document-wide
    parity (every run toggling one shared flag) flips there and silently misreads
    the rest of that table, so equal-length pairing is what keeps the mask
    honest. A fenced-code delimiter toggles the fenced state -- fence backticks
    are structural, never inline-span delimiters.
    """
    masks = [[False] * len(line) for line in doc_lines]
    open_runs: dict = {}   # run length -> (line index, column just past the opener)
    in_fence = False
    for idx, line in enumerate(doc_lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            open_runs.clear()
            continue
        if in_fence:
            open_runs.clear()   # fenced sample code is not prose
            continue
        if _ends_block(line):
            open_runs.clear()   # a span never crosses a blank line or a heading
        i = 0
        n = len(line)
        while i < n:
            if line[i] == "`" and not (i and line[i - 1] == "\\"):
                j = i
                while j < n and line[j] == "`":
                    j += 1
                opener = open_runs.pop(j - i, None)
                if opener is None:
                    open_runs[j - i] = (idx, j)
                else:
                    o_line, o_col = opener
                    for k in range(o_line, idx + 1):
                        lo = o_col if k == o_line else 0
                        hi = i if k == idx else len(masks[k])
                        for p in range(lo, min(hi, len(masks[k]))):
                            masks[k][p] = True
                i = j
                continue
            i += 1
    return masks


def _inside_code(code_spans: list, line_index: int, pos: int) -> bool:
    if line_index >= len(code_spans):
        return False
    row = code_spans[line_index]
    return bool(row[pos]) if 0 <= pos < len(row) else False


def extract(doc_lines: list, code_spans: list) -> tuple:
    """Return (citations, bare references rejected for being outside a code span).

    SHORT binding follows the three rules in the module docstring: a naming that
    PRECEDES the SHORT on its own line wins, then the enclosing heading section,
    and a table row may bind only from its own line. A file named after the SHORT
    on the same line never binds it. A SHORT that none of the rules binds keeps
    an empty ``target``, which ``verify`` turns into an ``unresolved`` failure.
    """
    cites: list = []
    bare_outside: list = []
    section_last: str = ""

    for i, line in enumerate(doc_lines, start=1):
        if ATX_HEADING_RE.match(line):
            section_last = ""   # a heading ends the section -- nothing inherits across
        is_table_row = bool(TABLE_ROW_RE.match(line))

        # Rule (a)'s candidate namings: FULL citations plus BARE paths that sit
        # inside a code span, in position order so a SHORT can take the nearest
        # PRECEDING one. Nothing after the SHORT on its own line ever binds it.
        full_spans = [m.span() for m in FULL_RE.finditer(line)]
        line_refs = [(m.start(), m.group(1)) for m in FULL_RE.finditer(line)]
        bare_in_code = set()
        for m in BARE_RE.finditer(line):
            if any(s <= m.start() < e for s, e in full_spans):
                continue   # the path half of a FULL citation
            if not _inside_code(code_spans, i - 1, m.start()):
                continue
            bare_in_code.add(m.start())
            if m.group(0).rsplit("/", 1)[-1] in RUNTIME_ARTEFACTS:
                continue   # names no repo file, so it is never a baseline
            line_refs.append((m.start(), m.group(0)))
        line_refs.sort()

        matches = [(m.start(), "FULL", m) for m in FULL_RE.finditer(line)]
        for m in SHORT_RE.finditer(line):
            if any(s <= m.start() < e for s, e in full_spans):
                continue
            matches.append((m.start(), "SHORT", m))
        for m in BARE_RE.finditer(line):
            if any(s <= m.start() < e for s, e in full_spans):
                continue
            matches.append((m.start(), "BARE", m))

        for pos, kind, m in sorted(matches, key=lambda t: t[0]):
            if kind == "FULL":
                target = m.group(1)
                nums = tuple(int(g) for g in (m.group(2), m.group(3)) if g)
                section_last = target
            elif kind == "SHORT":
                before = [p for at, p in line_refs if at < pos]
                if before:                      # rule (a), nearest PRECEDING naming
                    target = before[-1]
                elif is_table_row:              # rule (c): rows never inherit
                    target = ""
                else:                           # rule (b): the heading section
                    target = section_last
                nums = tuple(int(g) for g in (m.group(1), m.group(2)) if g)
            else:
                cite = Citation(doc_line=i, raw=m.group(0), kind=kind, target=m.group(0), lines=())
                if m.start() not in bare_in_code:
                    bare_outside.append(cite)
                    continue
                cites.append(cite)
                # A runtime artefact names no repo file, so it never becomes the
                # section baseline a later `:123` would inherit.
                if m.group(0).rsplit("/", 1)[-1] not in RUNTIME_ARTEFACTS:
                    section_last = m.group(0)
                continue
            cites.append(Citation(doc_line=i, raw=m.group(0), kind=kind, target=target, lines=nums))
    return cites, bare_outside


def verify(cites: list, basenames: dict, root: Path = ROOT) -> None:
    cache: dict = {}
    for c in cites:
        if c.kind == "SHORT" and not c.target:
            c.exists = False
            c.in_bounds = False
            c.note = "shorthand citation with no file to inherit in its section"
            continue

        target = c.target.replace("\\", "/")
        if c.kind == "BARE" and target.rsplit("/", 1)[-1] in RUNTIME_ARTEFACTS:
            c.note = "runtime artefact (vault worker dir), not a repo file -- excluded"
            c.exists = True
            c.in_bounds = True
            c.resolved = "(runtime artefact)"
            continue
        candidates = [root / target]
        if "/" not in target:
            candidates = basenames.get(target, [])
            c.n_matches = len(candidates)
        found = [p for p in candidates if p.is_file()]

        if not found:
            c.exists = False
            c.in_bounds = False
            c.note = "file not found in worktree"
            continue

        path = found[0]
        c.exists = True
        c.resolved = str(path.relative_to(root))
        if path not in cache:
            cache[path] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        c.n_lines = cache[path]

        if c.lines:
            out = [n for n in c.lines if not (1 <= n <= c.n_lines)]
            c.in_bounds = not out
            if out:
                c.note = "line(s) out of bounds: " + ", ".join(str(n) for n in out)
        else:
            c.in_bounds = True  # bare reference: existence is the whole check


def _display(path: Path, root: Path = ROOT) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def check_doc(doc: Path, root: Path = ROOT) -> DocResult:
    """Extract and verify every citation in one document.

    Returns a ``DocResult`` carrying the citation list plus the ``missing``,
    ``out_of_bounds``, ``unresolved`` and ``excluded`` buckets, and the bare
    references that were rejected for sitting outside an inline code span.
    """
    doc_lines = doc.read_text(encoding="utf-8").splitlines()
    cites, bare_outside = extract(doc_lines, _code_spans(doc_lines))
    verify(cites, _basename_index(root), root)

    unresolved = [c for c in cites if c.kind == "SHORT" and not c.target]
    excluded = [c for c in cites if c.note.startswith("runtime artefact")]
    missing = [c for c in cites if not c.exists and c not in unresolved and c not in excluded]
    out_of_bounds = [c for c in cites if c.exists and not c.in_bounds]

    return DocResult(
        doc=doc,
        label=_display(doc, root),
        n_lines=len(doc_lines),
        doc_lines=doc_lines,
        cites=cites,
        missing=missing,
        out_of_bounds=out_of_bounds,
        unresolved=unresolved,
        excluded=excluded,
        bare_outside_code=bare_outside,
    )


def _show(c: Citation) -> str:
    lines = ",".join(str(n) for n in c.lines) if c.lines else "-"
    return (
        f"[{c.kind:5}] doc:{c.doc_line:<4} {c.raw:<52} -> {c.resolved or c.target:<58} "
        f"lines={lines:<12} file_lines={c.n_lines:<6} "
        f"exists={'Y' if c.exists else 'N'} in_bounds={'Y' if c.in_bounds else 'N'}"
        + (f"  # {c.note}" if c.note else "")
    )


def _report_doc(result: DocResult, verbose: bool) -> None:
    print("=" * 160)
    print(f"doc: {result.label}  ({result.n_lines} lines)")
    print(f"citations extracted: {len(result.cites)}  "
          f"(FULL={sum(c.kind == 'FULL' for c in result.cites)}, "
          f"SHORT={sum(c.kind == 'SHORT' for c in result.cites)}, "
          f"BARE={sum(c.kind == 'BARE' for c in result.cites)})")
    print(f"  bare refs in prose (outside backtick spans, not citations): "
          f"{len(result.bare_outside_code)}")
    print("=" * 160)
    for c in result.cites:
        print(_show(c))
    if verbose:
        print("-" * 160)
        print("BARE refs in prose -- outside a backtick span, therefore NOT citations")
        for c in result.bare_outside_code:
            print(f"  doc:{c.doc_line}: {c.raw}")

    print("=" * 160)
    print(f"TOTAL checked: {len(result.cites)}")
    print(f"  files missing      : {len(result.missing)}")
    print(f"  lines out of bounds: {len(result.out_of_bounds)}")
    print(f"  unresolved shorthand: {len(result.unresolved)}")
    print(f"  excluded (runtime) : {len(result.excluded)}")
    print(f"  clean              : {result.clean}")
    if result.failures:
        print("-" * 160)
        print("FAILURES (verbatim)")
        for c in result.failures:
            print(f"  doc:{c.doc_line}: {c.raw}   -- {c.note}"
                  + (f" (file has {c.n_lines} lines)" if c.n_lines else "")
                  + f"\n      | {result.doc_lines[c.doc_line - 1].strip()}")
    else:
        print(f"RESULT: OK ({result.label}) -- every citation resolves to an "
              f"existing file with in-bounds lines")


def _parse_args(args: list) -> tuple:
    """Hand-rolled parsing: repeatable ``--doc PATH`` / ``--doc=PATH`` plus ``-v``."""
    verbose = False
    docs: list = []
    unknown: list = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-v", "--verbose"):
            verbose = True
        elif arg == "--doc":
            if i + 1 >= len(args):
                unknown.append("--doc requires a PATH argument")
            else:
                docs.append(Path(args[i + 1]))
                i += 1
        elif arg.startswith("--doc="):
            value = arg.split("=", 1)[1]
            if not value:
                unknown.append("--doc= requires a PATH argument")
            else:
                docs.append(Path(value))
        else:
            unknown.append(f"unknown argument: {arg}")
        i += 1
    return verbose, docs, unknown


def main(argv: list | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    verbose, docs, unknown = _parse_args(args)
    if unknown:
        for message in unknown:
            print(message)
        print("usage: verify_doc_citations.py [-v] [--doc PATH]...")
        return 2
    if not docs:
        docs = [DOC]

    results: list = []
    for doc in docs:
        if not doc.is_file():
            print("=" * 160)
            print(f"doc: {_display(doc)}  -- FILE NOT FOUND")
            results.append(None)
            continue
        result = check_doc(doc)
        results.append(result)
        _report_doc(result, verbose)

    print("=" * 160)
    total = sum(len(r.cites) for r in results if r is not None)
    failures = sum(len(r.failures) for r in results if r is not None)
    failures += sum(1 for r in results if r is None)
    print(f"AGGREGATE: documents={len(results)} citations={total} failures={failures}")
    if failures:
        print("RESULT: FAIL")
        return 1
    print("RESULT: OK -- every citation in every document resolves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
