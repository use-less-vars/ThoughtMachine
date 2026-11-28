"""Tests for the pure KB-recall selector and its spawn-payload wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workspace.kb_recall import (
    MAX_TEXT_CHARS,
    MAX_TOKENS,
    query_tokens,
    select_recall,
    select_recall_with_stats,
)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _kb(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    return root


# ── tokens ───────────────────────────────────────────────────────────────────


def test_query_tokens_dedupes_lowercases_and_drops_short_identifiers():
    assert query_tokens("Review the WORKER spawn, worker! src/ a ab abc abcd") == [
        "review",
        "worker",
        "spawn",
        "abcd",
    ]


def test_query_tokens_capped_in_document_order():
    tokens = query_tokens(" ".join(f"token{i}z" for i in range(100)))
    assert len(tokens) == MAX_TOKENS
    assert tokens[0] == "token0z"
    assert tokens[-1] == f"token{MAX_TOKENS - 1}z"


def test_query_tokens_non_string_yields_empty():
    assert query_tokens(None) == []
    assert query_tokens(123) == []


# ── scope ────────────────────────────────────────────────────────────────────


def test_scans_only_project_markdown(tmp_path):
    kb = _kb(tmp_path)
    _write(kb / "project" / "lessons_learned.md", "the worker spawn contract\n")
    _write(kb / "personal" / "secrets.md", "worker spawn contract\n")
    _write(kb / "archive" / "old.md", "worker spawn contract\n")
    _write(kb / "project" / "notes.txt", "worker spawn contract\n")

    hits = select_recall(kb, "worker spawn contract")
    assert [hit["domain"] for hit in hits] == ["lessons_learned"]


def test_project_subdirectories_are_scanned(tmp_path):
    kb = _kb(tmp_path)
    _write(kb / "project" / "fixes" / "deep.md", "spawn contract fix\n")
    hits = select_recall(kb, "spawn contract")
    assert hits and hits[0]["domain"] == "deep"


# ── ranking / hit shape ──────────────────────────────────────────────────────


def test_ranking_prefers_more_distinct_tokens_then_path(tmp_path):
    kb = _kb(tmp_path)
    _write(kb / "project" / "a_two.md", "spawn contract\n")
    _write(kb / "project" / "b_one.md", "spawn only\n")
    _write(kb / "project" / "c_two.md", "contract spawn\n")

    hits = select_recall(kb, "spawn contract")
    # a_two and c_two tie on 2 tokens -> path order decides; b_one (1) last.
    assert [hit["domain"] for hit in hits] == ["a_two", "c_two", "b_one"]


def test_hit_shape_line_text_and_truncation(tmp_path):
    kb = _kb(tmp_path)
    _write(
        kb / "project" / "lessons_learned.md",
        "# Title\n\n   the worker spawn contract is x  \n" + "y" * 400 + " worker\n",
    )

    hits = select_recall(kb, "worker spawn contract")
    assert len(hits) == 1
    hit = hits[0]
    assert set(hit) == {"domain", "line", "text"}
    assert hit["domain"] == "lessons_learned"
    assert hit["line"] == 3
    assert hit["text"] == "the worker spawn contract is x"
    assert len(hit["text"]) <= MAX_TEXT_CHARS


def test_long_line_is_truncated_to_max_text_chars(tmp_path):
    kb = _kb(tmp_path)
    _write(kb / "project" / "long.md", "spawn " + "z" * 500 + "\n")
    hit = select_recall(kb, "spawn")[0]
    assert len(hit["text"]) == MAX_TEXT_CHARS


def test_most_distinctive_token_anchors_the_line(tmp_path):
    kb = _kb(tmp_path)
    # "spawn" is everywhere (not distinctive); "kbrecall" only in the later line.
    _write(kb / "project" / "a.md", "spawn\nspawn\n")
    _write(kb / "project" / "b.md", "spawn\nspawn\n")
    _write(kb / "project" / "c.md", "spawn\nspawn kbReCall here\n")
    hit = select_recall(kb, "spawn kbrecall")[0]
    assert hit["domain"] == "c"
    assert hit["line"] == 2


def test_selection_is_deterministic(tmp_path):
    kb = _kb(tmp_path)
    for name in ("b.md", "a.md", "c.md"):
        _write(kb / "project" / name, "spawn contract\n")
    first = select_recall(kb, "spawn contract")
    assert all(select_recall(kb, "spawn contract") == first for _ in range(3))
    assert [hit["domain"] for hit in first] == ["a", "b", "c"]


# ── bounds ───────────────────────────────────────────────────────────────────


def test_max_hits_caps_results(tmp_path):
    kb = _kb(tmp_path)
    for index in range(5):
        _write(kb / "project" / f"f{index}.md", "spawn contract\n")
    assert len(select_recall(kb, "spawn contract", max_hits=2)) == 2
    assert len(select_recall(kb, "spawn contract", max_hits=99)) == 5


def test_max_bytes_drops_worst_ranked_hits(tmp_path):
    kb = _kb(tmp_path)
    _write(kb / "project" / "best.md", "spawn contract " + "a" * 150 + "\n")
    _write(kb / "project" / "worse.md", "spawn " + "b" * 150 + "\n")

    everything = select_recall(kb, "spawn contract", max_hits=5, max_bytes=8000)
    assert [hit["domain"] for hit in everything] == ["best", "worse"]

    one_hit_bytes = len(json.dumps(everything[:1], ensure_ascii=False).encode("utf-8"))
    trimmed = select_recall(kb, "spawn contract", max_hits=5, max_bytes=one_hit_bytes)
    assert [hit["domain"] for hit in trimmed] == ["best"]
    assert len(json.dumps(trimmed, ensure_ascii=False).encode("utf-8")) <= one_hit_bytes


def test_zero_bounds_and_no_tokens_return_empty(tmp_path):
    kb = _kb(tmp_path)
    _write(kb / "project" / "lessons.md", "spawn contract\n")
    assert select_recall(kb, "spawn contract", max_hits=0) == []
    assert select_recall(kb, "spawn contract", max_bytes=0) == []
    assert select_recall(kb, "spawn contract", max_hits=-1) == []
    assert select_recall(kb, "a ab abc") == []
    assert select_recall(kb, "") == []


# ── failure policy / stats ───────────────────────────────────────────────────


def test_missing_or_unusable_kb_root_returns_empty(tmp_path):
    assert select_recall(tmp_path / "nope", "spawn contract") == []
    assert select_recall(None, "spawn contract") == []
    assert select_recall(tmp_path / "file.md", "spawn contract") == []


def test_unreadable_file_is_skipped(tmp_path, monkeypatch):
    kb = _kb(tmp_path)
    _write(kb / "project" / "good.md", "spawn contract\n")
    broken = _write(kb / "project" / "broken.md", "spawn contract\n")

    real_read_text = Path.read_text

    def _read_text(self, *args, **kwargs):
        if self == broken:
            raise OSError("boom")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)

    hits, stats = select_recall_with_stats(kb, "spawn contract")
    assert [hit["domain"] for hit in hits] == ["good"]
    assert stats["files_failed"] == 1
    assert stats["files_scanned"] == 1


def test_large_file_is_counted_and_not_read(tmp_path):
    kb = _kb(tmp_path)
    _write(kb / "project" / "small.md", "spawn contract\n")
    big = kb / "project" / "big.md"
    big.write_text("spawn contract\n", encoding="utf-8")
    with big.open("ab") as handle:
        handle.write(b"#" * (300 * 1024))

    hits, stats = select_recall_with_stats(kb, "spawn contract")
    assert [hit["domain"] for hit in hits] == ["small"]
    assert stats["files_skipped_large"] == 1
    assert stats["files_scanned"] == 1


def test_never_raises_on_garbage_input():
    for garbage in (object(), 3.5, [], {"a": 1}, b"spawn"):
        assert select_recall(garbage, "spawn contract") == []


# ── spawn-payload wiring ─────────────────────────────────────────────────────


def test_spawn_recall_fills_slot_from_workspace_kb(tmp_path):
    from types import SimpleNamespace

    from tools.workspace.worker_thread import Worker

    _write(
        tmp_path / ".thoughtmachine" / "knowledge" / "project" / "lessons_learned.md",
        "the worker spawn contract\n",
    )
    fake_self = SimpleNamespace(agent_config={}, workspace_path=str(tmp_path))

    hits = Worker._spawn_kb_recall(fake_self, {"query": "worker spawn contract"})
    assert [hit["domain"] for hit in hits] == ["lessons_learned"]


def test_spawn_recall_knobs_disable_injection(tmp_path):
    from types import SimpleNamespace

    from tools.workspace.worker_thread import Worker

    _write(
        tmp_path / ".thoughtmachine" / "knowledge" / "project" / "lessons.md",
        "worker spawn contract\n",
    )
    payload = {"query": "worker spawn contract"}

    off = SimpleNamespace(
        agent_config={"kb_injection_max_hits": 0}, workspace_path=str(tmp_path)
    )
    assert Worker._spawn_kb_recall(off, payload) == []

    off_bytes = SimpleNamespace(
        agent_config={"session_config": {"kb_injection_max_bytes": 0}},
        workspace_path=str(tmp_path),
    )
    assert Worker._spawn_kb_recall(off_bytes, payload) == []


def test_spawn_recall_uses_the_briefing_when_the_query_has_no_identifiers(tmp_path):
    from types import SimpleNamespace

    from tools.workspace.worker_thread import Worker

    _write(
        tmp_path / ".thoughtmachine" / "knowledge" / "project" / "notes.md",
        "the hollow_promise flag\n",
    )
    fake_self = SimpleNamespace(agent_config={}, workspace_path=str(tmp_path))

    assert Worker._spawn_kb_recall(fake_self, {"query": "go"}) == []
    hits = Worker._spawn_kb_recall(
        fake_self, {"query": "go", "briefing": "check hollow_promise here"}
    )
    assert [hit["domain"] for hit in hits] == ["notes"]


def test_spawn_recall_never_breaks_a_spawn(tmp_path):
    from types import SimpleNamespace

    from tools.workspace.worker_thread import Worker

    # No workspace_path at all, and a knob that cannot be coerced to int.
    assert Worker._spawn_kb_recall(SimpleNamespace(agent_config={}), {}) == []
    assert (
        Worker._spawn_kb_recall(
            SimpleNamespace(
                agent_config={"kb_injection_max_hits": "lots"},
                workspace_path=str(tmp_path),
            ),
            {"query": "worker spawn"},
        )
        == []
    )
