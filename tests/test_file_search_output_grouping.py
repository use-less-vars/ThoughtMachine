"""Output shape of ``FileSearchTool``.

The contract pinned here is about HONESTY of the rendered search output:

* one ``rel_path:`` header per file (not one per match),
* context windows of nearby matches merged so no source line prints twice,
* a small per-file cap on printed blocks PLUS an explicit notice naming how
  many matches were omitted and on which lines (a silent cap is a lie),
* the ``MAX_OUTPUT_CHARS`` budget is really respected, and when it bites the
  output says so and how many matches were dropped -- and context is spent
  last, so many distinct matches beat one match's surroundings.

Run:  python3 -m pytest tests/test_file_search_output_grouping.py -v
"""

import re

import pytest

from tools.file_search_tool import FileSearchTool


def _search(root, **kwargs):
    params = dict(
        pattern="needle",
        directory=str(root),
        file_pattern="*.txt",
        workspace_path=str(root),
        effective_permissions={"filesystem": "read"},
        use_regex=False,
    )
    params.update(kwargs)
    return FileSearchTool(**params).execute()


@pytest.fixture()
def spaced(tmp_path):
    """``spaced.txt``: 20 matches on lines 2, 4, ... 40 (one per block at ctx 0)."""
    root = tmp_path / "spaced_ws"
    root.mkdir()
    lines = [
        "needle here\n" if n % 2 == 0 else "filler %d\n" % n
        for n in range(1, 41)
    ]
    (root / "spaced.txt").write_text("".join(lines))
    return root


@pytest.fixture()
def nearby(tmp_path):
    """``nearby.txt``: matches on lines 10 and 12, i.e. overlapping context."""
    root = tmp_path / "nearby_ws"
    root.mkdir()
    lines = ["line %d\n" % n for n in range(1, 31)]
    lines[9] = "needle ten\n"
    lines[11] = "needle twelve\n"
    (root / "nearby.txt").write_text("".join(lines))
    return root


@pytest.fixture()
def big(tmp_path):
    """``big.txt``: 200 matches spread over 400 lines."""
    root = tmp_path / "big_ws"
    root.mkdir()
    lines = [
        "needle\n" if n % 2 == 0 else "filler %d\n" % n
        for n in range(1, 401)
    ]
    (root / "big.txt").write_text("".join(lines))
    return root


def test_path_is_printed_once_per_file(spaced):
    out = _search(spaced, context_lines=0)

    assert "Found 20 matches" in out
    assert out.count("spaced.txt") == 1, out


def test_omitted_matches_are_named_with_count_and_lines(spaced):
    out = _search(spaced, context_lines=0)

    # The 5 printed blocks are the matches on lines 2..10; the other 15 are
    # announced explicitly rather than silently dropped.
    assert (
        "[+15 more matches in this file at lines "
        "12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40]"
    ) in out, out
    assert out.count(">") == 5, out


def test_overlapping_context_windows_print_a_shared_line_once(nearby):
    out = _search(nearby, context_lines=5)

    assert out.count("line 11") == 1, out
    assert out.count("**needle**") == 2, out
    assert out.count("nearby.txt") == 1, out


def test_small_search_keeps_line_numbers_and_highlight(tmp_path):
    root = tmp_path / "small_ws"
    root.mkdir()
    lines = ["filler %d\n" % n for n in range(1, 21)]
    lines[6] = "hay needle here\n"
    (root / "small.txt").write_text("".join(lines))

    out = _search(root, context_lines=1)

    assert ">    7: hay **needle** here" in out, out
    assert "     6: filler 6" in out, out


def test_character_budget_is_respected_and_reported(big, monkeypatch):
    monkeypatch.setattr(FileSearchTool, "MAX_OUTPUT_CHARS", 2000)
    monkeypatch.setattr(FileSearchTool, "MAX_MATCHES_PER_FILE", 500)

    out = _search(big, context_lines=0, max_results=200)

    assert len(out) <= FileSearchTool.MAX_OUTPUT_CHARS, len(out)
    assert (
        "[Output truncated by the 2000 character budget" in out
    ), out
    dropped = int(re.search(r"budget: (\d+) further matches dropped", out).group(1))
    assert dropped > 0, out


def test_context_is_dropped_before_matches(big, monkeypatch):
    monkeypatch.setattr(FileSearchTool, "MAX_OUTPUT_CHARS", 2000)
    monkeypatch.setattr(FileSearchTool, "MAX_MATCHES_PER_FILE", 500)

    out = _search(big, context_lines=20, max_results=200)

    assert (
        "[Context lines dropped to fit the 2000 character budget" in out
    ), out
    # Many distinct matches survive; they are just shown without context.
    assert out.count("\n>") > 10, out
