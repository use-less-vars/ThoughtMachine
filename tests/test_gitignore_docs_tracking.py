"""Guards against tracked docs files being silently hidden from the RAG index.

``docs/*`` in ``.gitignore`` excludes the whole docs tree; the negations below
it re-include the files that are meant to be versioned.  This matters beyond
git hygiene: ``agent/knowledge/codebase_indexer.py`` builds its exclusion spec
from ``.gitignore`` with ``pathspec`` and never consults git tracking
(``codebase_indexer.py:200-211``), so a doc that is tracked *and* still
ignore-matched is silently absent from the RAG index.  These tests keep
``.gitignore`` honest about the docs tree it is supposed to publish.

The spec is obtained from the indexer's own ``load_gitignore_spec()`` rather
than a local imitation: a re-implementation would keep passing after the real
loader changed, which is exactly the drift this file exists to catch.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent.knowledge.codebase_indexer import load_gitignore_spec
from agent.knowledge.dependencies import check_rag_dependencies

REPO_ROOT = Path(__file__).resolve().parents[1]

check_rag_dependencies()


def _tracked_docs_files(root: Path) -> list[str]:
    """Repo-relative POSIX paths of every tracked file under ``docs/``."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "docs"],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )
    return sorted(path for path in result.stdout.split("\0") if path)


def test_gitignore_has_no_conflict_markers() -> None:
    """An unresolved merge hunk silently disables every pattern inside it."""
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    markers = [
        f"{lineno}: {line}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if line.startswith("<<<<<<< ")
        or line.startswith(">>>>>>> ")
        or line.rstrip() == "======="
    ]
    assert markers == [], (
        ".gitignore contains unresolved merge-conflict markers, which make "
        f"patterns inside the hunk behave unpredictably: {markers}"
    )


def test_no_tracked_docs_file_is_ignore_matched() -> None:
    """Every tracked doc must reach the indexer's inclusion set."""
    if shutil.which("git") is None:
        pytest.skip("git binary unavailable in this container")

    tracked = _tracked_docs_files(REPO_ROOT)
    if not tracked:
        pytest.skip("no tracked files under docs/")

    spec = load_gitignore_spec(REPO_ROOT)
    assert spec is not None, (
        "load_gitignore_spec() returned None, so the indexer applies no "
        ".gitignore filtering and this guard cannot be evaluated"
    )

    hidden = [path for path in tracked if spec.match_file(path)]
    assert hidden == [], (
        "tracked docs files are still matched by .gitignore and are therefore "
        f"invisible to the RAG index (codebase_indexer.py:200-211): {hidden}"
    )
