"""Workspace-scoped knowledge-base recall (pure selection, no side effects).

Given a KB root and the text of a spawn query, pick the few ``project/``
markdown files that best match it and return one compact pointer per file so a
worker can decide for itself whether to open it.

Deliberately PURE: file reads and string work only -- no tool, no session, no
permissions, no cache, no network, no embedding.  That is what makes it safe to
call from the spawn path (where a failure must never break the spawn) and
trivial to test.

Scope: ONLY ``<kb_root>/project/**/*.md``.  ``personal/``, ``archive/`` and any
sibling of ``kb_root`` are never scanned.

The function never raises: a missing/unreadable ``kb_root`` yields ``[]`` and an
unreadable individual file is skipped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

#: Identifier-ish query tokens: a letter/underscore then >=3 more chars.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")

#: Query tokens considered at all (first N in document order, deduped).
MAX_TOKENS = 60

#: Files bigger than this are reported as skipped and never read.
MAX_FILE_BYTES = 256 * 1024

#: A recalled line is a pointer, not a document: hard cap on its text.
MAX_TEXT_CHARS = 200

DEFAULT_MAX_HITS = 3
DEFAULT_MAX_BYTES = 1200


def query_tokens(query_text: Any) -> List[str]:
    """Return the deduped, lowercased identifier tokens of ``query_text``.

    Order is document order of first appearance; at most ``MAX_TOKENS`` are
    returned.  A non-string ``query_text`` yields ``[]``.
    """
    if not isinstance(query_text, str):
        return []
    seen = set()
    out: List[str] = []
    for match in _TOKEN_RE.finditer(query_text):
        token = match.group(0).lower()
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= MAX_TOKENS:
            break
    return out


def _project_files(kb_root: Path) -> List[Tuple[str, Path]]:
    """Return ``(relative_posix_path, path)`` for ``<kb_root>/project/**/*.md``.

    Sorted by relative path so every later step is deterministic.  Any
    filesystem error yields ``[]``.
    """
    project = kb_root / "project"
    try:
        if not project.is_dir():
            return []
        found: List[Tuple[str, Path]] = []
        for path in project.rglob("*.md"):
            try:
                if not path.is_file():
                    continue
                rel = path.relative_to(project).as_posix()
            except (OSError, ValueError):
                continue
            found.append((rel, path))
    except OSError:
        return []
    found.sort(key=lambda item: item[0])
    return found


def _scan(
    kb_root: Any,
    query_text: Any,
    max_hits: int = DEFAULT_MAX_HITS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Core of :func:`select_recall`, returning ``(hits, stats)``."""
    stats = {"files_scanned": 0, "files_failed": 0, "files_skipped_large": 0}
    try:
        if max_hits <= 0 or max_bytes <= 0:
            return [], stats
        tokens = query_tokens(query_text)
        if not tokens:
            return [], stats
        try:
            root = Path(kb_root)
        except (TypeError, ValueError):
            return [], stats
        files = _project_files(root)
        if not files:
            return [], stats

        # Per file: the lowercased text and its lines (kept for the line lookup).
        texts: List[Tuple[str, List[str]]] = []
        paths: List[str] = []
        for rel, path in files:
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    stats["files_skipped_large"] += 1
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stats["files_failed"] += 1
                continue
            except Exception:  # pragma: no cover - defensive, never raise out
                stats["files_failed"] += 1
                continue
            stats["files_scanned"] += 1
            paths.append(rel)
            texts.append((text.lower(), text.splitlines()))

        if not stats["files_scanned"]:
            return [], stats

        # Document frequency per token: the rarest matched token is the most
        # distinctive one, and its first occurrence anchors the hit.
        frequencies: Dict[str, int] = {}
        for token in tokens:
            frequencies[token] = sum(
                1 for lowered, _lines in texts if token in lowered
            )

        candidates: List[Tuple[int, str, int, Dict[str, Any]]] = []
        for (rel, (lowered, lines)) in zip(paths, texts):
            matched = [token for token in tokens if token in lowered]
            if not matched:
                continue
            distinctive = min(matched, key=lambda tok: (frequencies[tok], tok))
            line_no = 0
            for index, raw in enumerate(lines, start=1):
                if distinctive in raw.lower():
                    line_no = index
                    break
            if line_no <= 0:
                continue
            candidates.append((
                -len(matched),
                rel,
                line_no,
                {
                    "domain": Path(rel).stem,
                    "line": line_no,
                    "text": lines[line_no - 1].strip()[:MAX_TEXT_CHARS],
                },
            ))

        # count desc, path asc, earliest matching line asc -- fully deterministic.
        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]["domain"]))

        hits = [item[3] for item in candidates[:max_hits]]
        # Drop from the worst-ranked end until the serialised total fits.
        while hits and len(
            json.dumps(hits, ensure_ascii=False).encode("utf-8")
        ) > max_bytes:
            hits.pop()
        return hits, stats
    except Exception:  # pragma: no cover - the contract is "never raise"
        return [], stats


def select_recall(
    kb_root: Any,
    query_text: Any,
    max_hits: int = DEFAULT_MAX_HITS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> List[Dict[str, Any]]:
    """Return up to ``max_hits`` recall pointers for ``query_text``.

    Each hit is ``{"domain": <file stem>, "line": <1-based line>, "text":
    <that line stripped, <=200 chars>}``.  One hit per file, ranked by the
    number of distinct query tokens the file contains (desc), then path (asc),
    then line (asc), then domain (asc).

    ``max_hits <= 0``, ``max_bytes <= 0``, no usable query tokens or an
    unusable ``kb_root`` all yield ``[]``.  Never raises.
    """
    return _scan(kb_root, query_text, max_hits=max_hits, max_bytes=max_bytes)[0]


def select_recall_with_stats(
    kb_root: Any,
    query_text: Any,
    max_hits: int = DEFAULT_MAX_HITS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Same as :func:`select_recall`, plus ``files_scanned``/``files_failed``/
    ``files_skipped_large`` counters."""
    return _scan(kb_root, query_text, max_hits=max_hits, max_bytes=max_bytes)
