#!/usr/bin/env python3
"""Mechanically verify every file citation in docs/design-worker-lifecycle.md.

Extracted citation forms
------------------------
1. FULL   ``path/to/file.py:123`` and ``path/to/file.py:123-456``
2. SHORT  ``:123`` / ``:123-456`` -- a continuation reference that inherits the
          file path from the nearest preceding FULL citation in the same line,
          or (when the line has none) from the nearest preceding FULL citation
          in the same contiguous block of non-blank lines.
3. BARE   a file path mentioned with no line number (``infra/container_manager.py``).
          A bare reference containing ``/`` is resolved as a worktree-relative
          path; a bare basename is resolved by basename lookup anywhere in the
          worktree (a spec may say ``worker_thread.py`` for
          ``tools/workspace/worker_thread.py``), and only fails when no file of
          that name exists at all.

Checks per citation: (a) the file exists, (b) every cited line number lies in
1..len(file lines). No sampling -- every match is emitted.

Usage:  python3 scripts/verify_doc_citations.py [-v]
Exit code 0 when every citation resolves, 1 otherwise.
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
RUNTIME_ARTEFACTS = {"command.json", "context.json", "status.json"}

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


def _basename_index() -> dict:
    index: dict = {}
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        index.setdefault(p.name, []).append(p)
    return index


def extract(doc_lines: list) -> list:
    cites: list = []
    last_full: str = ""

    for i, line in enumerate(doc_lines, start=1):
        # Positional order matters: a shorthand `:123` inherits the file of the
        # nearest full citation that PRECEDES it, not merely one on the same line.
        matches = []
        full_spans = []
        for m in FULL_RE.finditer(line):
            full_spans.append(m.span())
            matches.append((m.start(), "FULL", m))
        for m in SHORT_RE.finditer(line):
            if any(s <= m.start() < e for s, e in full_spans):
                continue
            matches.append((m.start(), "SHORT", m))
        for m in BARE_RE.finditer(line):
            if any(s <= m.start() < e for s, e in full_spans):
                continue
            matches.append((m.start(), "BARE", m))

        for _pos, kind, m in sorted(matches, key=lambda t: t[0]):
            if kind == "FULL":
                target = m.group(1)
                nums = tuple(int(g) for g in (m.group(2), m.group(3)) if g)
                last_full = target
            elif kind == "SHORT":
                target = last_full
                nums = tuple(int(g) for g in (m.group(1), m.group(2)) if g)
            else:
                target = m.group(0)
                nums = ()
            cites.append(Citation(doc_line=i, raw=m.group(0), kind=kind, target=target, lines=nums))
    return cites


def verify(cites: list, basenames: dict) -> None:
    cache: dict = {}
    for c in cites:
        if c.kind == "SHORT" and not c.target:
            c.exists = False
            c.in_bounds = False
            c.note = "shorthand citation with no preceding full path to inherit"
            continue

        target = c.target.replace("\\", "/")
        if c.kind == "BARE" and target.rsplit("/", 1)[-1] in RUNTIME_ARTEFACTS:
            c.note = "runtime artefact (vault worker dir), not a repo file -- excluded"
            c.exists = True
            c.in_bounds = True
            c.resolved = "(runtime artefact)"
            continue
        candidates = [ROOT / target]
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
        c.resolved = str(path.relative_to(ROOT))
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


def main() -> int:
    verbose = "-v" in sys.argv[1:]
    doc_lines = DOC.read_text(encoding="utf-8").splitlines()
    basenames = _basename_index()
    cites = extract(doc_lines)
    verify(cites, basenames)

    def show(c: Citation) -> str:
        lines = ",".join(str(n) for n in c.lines) if c.lines else "-"
        return (
            f"[{c.kind:5}] doc:{c.doc_line:<4} {c.raw:<52} -> {c.resolved or c.target:<58} "
            f"lines={lines:<12} file_lines={c.n_lines:<6} "
            f"exists={'Y' if c.exists else 'N'} in_bounds={'Y' if c.in_bounds else 'N'}"
            + (f"  # {c.note}" if c.note else "")
        )

    print(f"doc: {DOC.relative_to(ROOT)}  ({len(doc_lines)} lines)")
    print(f"citations extracted: {len(cites)}  "
          f"(FULL={sum(c.kind == 'FULL' for c in cites)}, "
          f"SHORT={sum(c.kind == 'SHORT' for c in cites)}, "
          f"BARE={sum(c.kind == 'BARE' for c in cites)})")
    print("=" * 160)
    for c in cites:
        if verbose or c.raw:
            print(show(c))

    missing = [c for c in cites if not c.exists and not c.note.startswith("runtime artefact")]
    excluded = [c for c in cites if c.note.startswith("runtime artefact")]
    oob = [c for c in cites if c.exists and not c.in_bounds]
    print("=" * 160)
    print(f"TOTAL checked: {len(cites)}")
    print(f"  files missing      : {len(missing)}")
    print(f"  lines out of bounds: {len(oob)}")
    print(f"  excluded (runtime) : {len(excluded)}")
    print(f"  clean              : {len(cites) - len(missing) - len(oob) - len(excluded)}")
    if missing or oob:
        print("-" * 160)
        print("FAILURES (verbatim)")
        for c in missing + oob:
            print(f"  doc:{c.doc_line}: {c.raw}   -- {c.note}"
                  + (f" (file has {c.n_lines} lines)" if c.n_lines else "")
                  + f"\n      | {doc_lines[c.doc_line - 1].strip()}")
        return 1
    print("RESULT: OK -- every citation resolves to an existing file with in-bounds lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
