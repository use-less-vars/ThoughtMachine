"""``diff`` ``name_only=True``: the LIST of changed paths, honestly bounded.

Seam: the tool never spawns a subprocess on its own -- every git invocation
goes through ``GitReadTool._run_git_raw(repo_root, args, timeout=...)``, which
hands the fixed argv to the container/host backend and returns
``(exit_code, stdout, stderr)``. These tests stub exactly that seam (so no git
binary and no docker daemon are needed) and assert on the argv the tool built.
``_validate_repo_root`` is stubbed too: no workspace is bound in a unit test,
so git repository containment is not what is under test here.
"""

from pathlib import Path

import pytest

import tools.git_info_tool as git_info_tool
from tools.git_info_tool import GitInfoTool

_HEADER_PREFIXES = (
    "changed_paths:",
    "complete:",
    "execution_mode:",
    "failure_reason:",
    "(",
)


def _stub_git(monkeypatch, stdout, seen):
    """Patch the subprocess seam and repo-root containment."""
    def _raw(self, repo_root, args, timeout=30):
        seen.append(list(args))
        return (0, stdout, "")

    monkeypatch.setattr(GitInfoTool, "_run_git_raw", _raw, raising=True)
    monkeypatch.setattr(
        GitInfoTool, "_validate_repo_root", lambda self, root: Path(root),
        raising=True,
    )


def _tool(**kwargs):
    tool = GitInfoTool(operation="diff", **kwargs)
    tool._last_execution_mode = "host"
    tool._last_failure_reason = None
    return tool


def _path_lines(output):
    return [
        line for line in output.splitlines()
        if line.strip() and not line.startswith(_HEADER_PREFIXES)
    ]


def test_name_only_returns_one_changed_path_per_line(monkeypatch, tmp_path):
    seen = []
    _stub_git(monkeypatch, "docs/a.md\nsrc/b.py\n", seen)

    output = _tool(name_only=True)._git_diff(tmp_path)

    assert _path_lines(output) == ["docs/a.md", "src/b.py"]
    # --name-only is added to the SAME fixed argv by the tool
    assert seen[0] == ["diff", "--no-ext-diff", "--no-textconv", "--name-only"]


def test_name_only_reports_complete_true_and_path_count(monkeypatch, tmp_path):
    seen = []
    _stub_git(monkeypatch, "docs/a.md\nsrc/b.py\n", seen)

    output = _tool(name_only=True)._git_diff(tmp_path)

    assert "changed_paths: 2" in output.splitlines()
    assert "complete: true" in output.splitlines()
    assert "complete: false" not in output


def test_capped_name_only_list_is_not_presented_as_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(git_info_tool, "_NAME_ONLY_MAX_PATHS", 2, raising=True)
    seen = []
    _stub_git(monkeypatch, "f0.txt\nf1.txt\nf2.txt\nf3.txt\nf4.txt\n", seen)

    output = _tool(name_only=True)._git_diff(tmp_path)

    assert "changed_paths: 5" in output.splitlines()
    assert "complete: false" in output.splitlines()
    assert "complete: true" not in output
    assert "CAPPED" in output
    # the withheld paths are genuinely absent -- and said to be
    assert _path_lines(output) == ["f0.txt", "f1.txt"]
    assert "f4.txt" not in output


def test_patch_mode_is_unchanged_without_the_flag(monkeypatch, tmp_path):
    patch = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    seen = []
    _stub_git(monkeypatch, patch, seen)

    tool = _tool()
    assert tool.name_only is False
    output = tool._git_diff(tmp_path)

    assert "--name-only" not in seen[0]
    assert seen[0] == ["diff", "--no-ext-diff", "--no-textconv"]
    assert patch.rstrip("\n") in output
    assert "changed_paths:" not in output
    assert "complete:" not in output
