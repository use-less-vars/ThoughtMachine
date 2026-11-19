# tests/test_gen_handoff.py
"""Tests for ``scripts/gen_handoff.py`` -- the onboarding router generator (C37).

Synthetic fixtures only. **No git binary is required or allowed**: an autouse
fixture replaces ``gen_handoff.sh`` with a function that raises, so any path
that quietly acquired a dependency on git fails loudly (C34/C37). Nothing here
creates a temporary git repository; the one directory tree that is built is a
flat set of placeholder files.

What is pinned, and the live defect each pin comes from:

* a bold title WRAPPED over two physical lines still parses (C11 -- open the
  whole time and invisible to every generated handoff);
* every ``**Open.**`` block is emitted, and NO block carrying one of the five
  closed status tokens (``Landed`` / ``Done`` / ``FIXED`` / ``Disproved`` /
  ``CLOSED``) is emitted;
* an open item whose prose merely mentions a closed word stays OPEN (C33
  collided with itself this way and the parse fell from 13 items to 12);
* an item filed BELOW the ``## Archived`` heading is still parsed (C12, C14 and
  the C74-C92 residue were never named by a generated handoff);
* the dash title form ``- **C78** -- Title.`` parses at all (it was invisible
  to the colon-only title regex);
* the emitted id set equals the parsed id set -- no ``open_items[:3]``-style
  truncation;
* zero open items is a REFUSAL, not an empty queue.

The queue-body assertions run through the real ``render()`` loop with pinned
git facts, so what is checked is the bytes the router would ship, not a
re-implementation of them.
"""
import io
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_handoff  # noqa: E402  (imported after the sys.path insert, on purpose)

#: The heading pair that brackets the queue body. The first is load-bearing
#: outside this file too: tests/test_onboarding_bundle_contract.py slices on it.
QUEUE_HEADING = "## First task (from the live queue)"
QUEUE_NEXT_HEADING = "## Rules of Engagement (Gen 62 Protocol)"

ARCHIVED_HEADING = "## Archived (pre-Gen 42 brain dump — do not read as current work)"

#: The bullet shape the render loop emits, and the shape the contract test reads.
BULLET_RE = re.compile(r"^- \*\*(C\d+)\*\* -- ")

CLOSED_MARKERS = ("Landed", "Done", "FIXED", "Disproved", "CLOSED")


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    """Every test here must pass with no git binary on ``PATH`` (C34)."""

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on a regression
        raise AssertionError(
            f"gen_handoff shelled out to {args!r}; these tests must not need git"
        )

    monkeypatch.setattr(gen_handoff, "sh", _boom)


def _ids(items):
    return [cid for cid, _title in items]


def _queue_block(open_items):
    """The router's queue bullets, as emitted by the REAL render loop."""
    text = gen_handoff.render(
        REPO_ROOT,
        open_items,
        "00000000",
        "pinned subject",
        "00000000 pinned commit",
    )
    lines = text.splitlines()
    assert QUEUE_HEADING in lines, (
        "the queue heading moved; tests/test_onboarding_bundle_contract.py "
        "slices on it"
    )
    start = lines.index(QUEUE_HEADING)
    end = lines.index(QUEUE_NEXT_HEADING)
    return [line for line in lines[start + 1 : end] if line]


# ---------------------------------------------------------------------------
# (a) a bold title wrapped over two physical lines  (C11 regression)
# ---------------------------------------------------------------------------

WRAPPED_TITLE_QUEUE = """\
- **C11: `ContainerManager.exec()` abandons the PID by design; callers own
  cleanup, and three don't.** **Open.** The exec layer returns early.
- **C7: Second item.** **Open.** Body.
"""


def test_bold_title_wrapped_over_two_lines_parses():
    items = gen_handoff._parse_open_items(WRAPPED_TITLE_QUEUE)

    assert _ids(items) == ["C11", "C7"]
    assert "callers own cleanup, and three don't." in items[0][1]
    assert _queue_block(items)[0] == (
        "- **C11** -- `ContainerManager.exec()` abandons the PID by design; "
        "callers own cleanup, and three don't."
    )


# ---------------------------------------------------------------------------
# (b) every "**Open.**" block emitted; no closed-status block emitted
# ---------------------------------------------------------------------------

OPEN_ITEM = "- **C7: Open for business.** **Open.** Body.\n"


@pytest.mark.parametrize("marker", CLOSED_MARKERS)
def test_a_closed_status_token_keeps_the_item_off_the_router(marker):
    text = OPEN_ITEM + f"- **C99: Closed by a ruling.** **{marker}** in `deadbeef`.\n"

    items = gen_handoff._parse_open_items(text)

    assert _ids(items) == ["C7"], f"**{marker}** did not mark C99 closed"
    assert _queue_block(items) == ["- **C7** -- Open for business."]


def test_every_open_block_reaches_the_router():
    text = "".join(f"- **C{i}: Item {i}.** **Open.** Body {i}.\n" for i in (1, 2, 3))

    items = gen_handoff._parse_open_items(text)

    assert _ids(items) == ["C1", "C2", "C3"]
    assert _queue_block(items) == [
        "- **C1** -- Item 1.",
        "- **C2** -- Item 2.",
        "- **C3** -- Item 3.",
    ]


# ---------------------------------------------------------------------------
# (c) prose that merely mentions a closed word is still open
# ---------------------------------------------------------------------------


def test_prose_that_merely_mentions_a_closed_word_stays_open():
    """Both real shapes: the bare word in prose, and the bold-but-not-a-status
    ``**Fix landed:**`` that C33's own body carries.
    """
    text = (
        "- **C33: Parser re-slices the queue.** **Open.** All three defects are"
        " fixed in the branch, so most of this Landed; the entry stays open"
        " until the merge SHA is known. **Fix landed:** the block is the unit"
        " now.\n"
    )

    items = gen_handoff._parse_open_items(text)

    assert _ids(items) == ["C33"]
    assert _queue_block(items) == ["- **C33** -- Parser re-slices the queue."]


# ---------------------------------------------------------------------------
# (d) the dash title form, and items filed BELOW the archive heading
# ---------------------------------------------------------------------------


def test_the_dash_title_form_is_parsed():
    items = gen_handoff._parse_open_items(
        "- **C78** — Preflight diagnostics truth: two port probes.\n"
    )

    assert _ids(items) == ["C78"]
    assert items[0][1] == "Preflight diagnostics truth: two port probes."


def test_an_item_below_the_archived_heading_is_still_parsed():
    text = (
        "- **C2: Above the fold.** **Open.** Body.\n"
        "\n"
        f"{ARCHIVED_HEADING}\n"
        "\n"
        "- **C12: `cancellation.interrupt()` sticky-fallback trap.** **Open.** Body.\n"
        "- **C78** — Below the fold.\n"
    )

    items = gen_handoff._parse_open_items(text)

    assert _ids(items) == ["C2", "C12", "C78"]


def test_main_hands_the_whole_file_to_the_parser(tmp_path, monkeypatch):
    """The regression for the defect itself: ``main()`` used to parse only the
    text above ``## Archived``, so an item filed below it could never be routed
    no matter what the parser could see.
    """
    seen = {}

    def _capture(text):
        seen["text"] = text
        return [("C7", "Open for business.")]

    monkeypatch.setattr(gen_handoff, "_parse_open_items", _capture)
    sink = type("_Sink", (), {"buffer": io.BytesIO(), "flush": lambda self: None})()
    monkeypatch.setattr(sys, "stdout", sink)
    recent = tmp_path / "recent-commits.txt"
    recent.write_text("00000000 pinned commit", encoding="utf-8")

    rc = gen_handoff.main(
        [
            "--root",
            str(REPO_ROOT),
            "--dev-sha",
            "00000000",
            "--dev-subject",
            "pinned subject",
            "--recent-file",
            str(recent),
            "--stdout",
        ]
    )

    assert rc == 0
    assert seen["text"] == (REPO_ROOT / gen_handoff.KB / "PRIORITIES.md").read_text(
        encoding="utf-8"
    ), "main() parsed a slice of PRIORITIES.md, not the file"
    assert "## Archived" in seen["text"], (
        "the whole-file text must still carry the archive block"
    )
    out = sink.buffer.getvalue().decode("utf-8")
    assert "- **C7** -- Open for business." in out


# ---------------------------------------------------------------------------
# (e) emitted ids == parsed ids (no truncation)
# ---------------------------------------------------------------------------


def test_emitted_ids_equal_parsed_ids():
    text = "".join(f"- **C{i}: Item {i}.** **Open.** Body {i}.\n" for i in range(1, 21))

    items = gen_handoff._parse_open_items(text)

    emitted = [BULLET_RE.match(line).group(1) for line in _queue_block(items)]
    assert emitted == _ids(items)
    assert emitted == [f"C{i}" for i in range(1, 21)]


def test_a_repeated_id_is_emitted_once():
    """The queue preserves duplicate historical bullets on purpose (the archived
    C35/C36 forms, the long/short C74-C77 pairs). An id names one item.
    """
    text = (
        "- **C74** — Long form of the item.\n"
        "- **C74** — Short duplicate.\n"
        "- **C75** — Only form.\n"
    )

    items = gen_handoff._parse_open_items(text)

    assert _ids(items) == ["C74", "C75"]
    assert items[0][1] == "Long form of the item."


# ---------------------------------------------------------------------------
# (f) zero open items is a refusal
# ---------------------------------------------------------------------------


def _fixture_tree(tmp_path, priorities_text):
    """A flat placeholder tree: enough for ``main()`` to reach the parse.

    No git repository and no git binary -- ``main()`` is handed every git-derived
    fact on the command line, exactly as the contract test does.
    """
    root = tmp_path / "tree"
    root.mkdir()
    for rel, _prose in gen_handoff.MANIFEST:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder\n", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "CONTROL_PLANE.md").write_text("placeholder\n", encoding="utf-8")
    (root / gen_handoff.KB / "PRIORITIES.md").write_text(priorities_text, encoding="utf-8")
    return root


def test_zero_open_items_is_a_refusal(tmp_path):
    root = _fixture_tree(
        tmp_path,
        "# PRIORITIES.md\n\n- **C1: Shipped.** **Landed** `fea41200`.\n",
    )
    recent = tmp_path / "recent-commits.txt"
    recent.write_text("00000000 pinned commit", encoding="utf-8")

    rc = gen_handoff.main(
        [
            "--root",
            str(root),
            "--dev-sha",
            "00000000",
            "--dev-subject",
            "pinned subject",
            "--recent-file",
            str(recent),
        ]
    )

    assert rc != 0, "an empty queue must refuse, not emit an empty First task list"
    assert not (root / gen_handoff.KB / "HANDOFF.md").exists()
