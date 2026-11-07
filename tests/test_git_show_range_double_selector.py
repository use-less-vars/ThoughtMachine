"""RED test -- B3: ``show`` line-range must refuse a ``commit`` carrying a selector.

``tools/git_info_tool.py::GitReadTool._git_show``'s LINE-RANGE branch builds the
git argument ``f"{self.commit}:{rel_paths[0]}"`` -- the tool itself supplies the
single ``:`` selector that separates a commit from a path.  When the caller's
``commit`` ALSO contains a ``:`` (e.g. ``commit="HEAD:README.md"``) the
constructed argument carries TWO selectors::

    commit="HEAD:README.md", file_path="README.md", line_start=2, line_end=4
        ->  git show --no-ext-diff --no-textconv "HEAD:README.md:README.md"

``_git_show`` never routes ``self.commit`` through ``_validate_git_ref``, and
``_validate_git_ref`` does not reject ``:`` anyway, so the doubled selector
reaches git.  A ``:`` in ``commit`` is never a legitimate selector for the range
branch and must be refused LOUDLY.

Only the RANGE branch is affected: the normal (non-range) path forwards
``self.commit`` and the paths as SEPARATE argv elements with a ``--`` separator,
so a ``:`` there is harmless and must keep working (anti-regression pin).

The tool's git execution seam (``_run_git`` / ``_run_git_raw``) is faked rather
than driven through a real ``git`` binary: this sandbox image has NO ``git``
executable, so a subprocess-driven setup would error for a reason UNRELATED to
the defect.  ``GitReadTool`` is a pydantic model, so the seam methods are
overridden with ``object.__setattr__`` (a plain instance assignment is rejected
by pydantic).

EXPECTED TODAY: RED -- the range-branch double-selector cases are all ACCEPTED
today (no refusal; the fabricated argument reaches the fake git seam), so the
loud-refusal assertion fails.  The legitimate-commit cases and the normal-path
case are GREEN today and pin the no-regression contract.
"""

from pathlib import Path

import pytest

from tools.git_info_tool import GitReadTool


def _tool(
    tmp_path: Path,
    argv: list,
    raw_output: str = "fake-out\n",
    **overrides,
) -> GitReadTool:
    """Build a ``show`` tool whose faked git seam records argv.

    Both ``_run_git`` and ``_run_git_raw`` append to the SAME ``argv`` list, so
    a test can assert what (if anything) reached git.  The range branch calls
    ``_run_git_raw`` (which must return an ``(exit_code, stdout, stderr)``
    triple); the normal path calls ``_run_git`` (which returns a string).
    """
    params = {
        "session_id": "s-b3",
        "workspace_id": "ws-b3",
        "session_permissions": {},
    }
    params.update(overrides)
    tool = GitReadTool(**params)

    # pydantic model: bypass validation to install the workspace context and
    # the faked git seam on the instance.
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "ws-b3")
    object.__setattr__(tool, "_use_container_mode", lambda: False)

    def fake_run_git(repo_root, args, timeout=30):
        argv.append(list(args))
        return raw_output

    def fake_run_git_raw(repo_root, args, timeout=30):
        argv.append(list(args))
        return (0, raw_output, "")

    object.__setattr__(tool, "_run_git", fake_run_git)
    object.__setattr__(tool, "_run_git_raw", fake_run_git_raw)
    return tool


@pytest.mark.parametrize(
    "commit",
    [
        "HEAD:README.md",
        "a0946fc:README.md",
        ":README.md",
        "dev:src/x.py",
    ],
)
def test_show_range_commit_with_selector_is_refused(tmp_path, commit):
    """A ``commit`` carrying its own ``:`` must be refused loudly (range branch).

    The range branch builds ``f"{self.commit}:{rel_paths[0]}"``; with a ``:``
    already in ``commit`` this yields a doubled selector
    (``HEAD:README.md:README.md``).  It must never reach git.
    """
    (tmp_path / "README.md").write_text("x\n")

    argv = []
    tool = _tool(
        tmp_path,
        argv,
        operation="show",
        commit=commit,
        file_path="README.md",
        line_start=2,
        line_end=4,
    )
    result = tool._git_show(tmp_path)

    # LOUD: a human-readable refusal (the exact vocabulary is not pinned here).
    assert result.strip().lower().startswith("error"), (
        f"commit={commit!r} carries its own ':' selector and the range branch "
        f"builds '<commit>:<path>' itself, so this must be refused loudly; "
        f"got: {result!r}"
    )

    # STRUCTURAL: never hand git an argument carrying a doubled selector.
    assert argv == [], f"expected no git call, got: {argv}"


@pytest.mark.parametrize(
    "commit",
    ["HEAD", "a0946fc", "release-1.0"],
)
def test_show_range_legitimate_commit_unchanged(tmp_path, commit):
    """A plain commit against a line range forwards exactly ``<commit>:<path>``."""
    (tmp_path / "README.md").write_text("x\n")

    argv = []
    tool = _tool(
        tmp_path,
        argv,
        raw_output="l1\nl2\nl3\nl4\nl5\n",
        operation="show",
        commit=commit,
        file_path="README.md",
        line_start=2,
        line_end=4,
    )
    result = tool._git_show(tmp_path)

    assert argv == [
        ["show", "--no-ext-diff", "--no-textconv", f"{commit}:README.md"]
    ], f"commit={commit!r} must be forwarded unchanged; got: {argv!r}"

    # _with_mode appends two diagnostics lines (execution_mode/failure_reason),
    # so full-string equality cannot hold; pin the sliced content exactly via
    # splitlines() instead of full-string equality.
    assert result.splitlines()[:3] == ["l2", "l3", "l4"], (
        f"the requested line range (2..4) must be sliced from the blob; "
        f"got: {result!r}"
    )
    assert "l1" not in result and "l5" not in result, (
        f"lines outside the requested range must be absent; got: {result!r}"
    )


def test_show_normal_path_colon_ref_still_forwarded(tmp_path):
    """Non-range path keeps ref and paths as SEPARATE argv elements (immune).

    The normal (non-range) path appends ``self.commit`` and the paths on their
    own with a ``--`` separator, so a ``:``-bearing commit is forwarded as one
    token and must NOT be refused.  A guard placed at the TOP of ``_git_show``
    (instead of inside ``if range_requested:``) would break this
    currently-working behaviour.
    """
    (tmp_path / "README.md").write_text("x\n")

    argv = []
    tool = _tool(
        tmp_path,
        argv,
        operation="show",
        commit="HEAD:README.md",
        file_path="README.md",
        line_start=None,
        line_end=None,
    )
    result = tool._git_show(tmp_path)

    assert argv == [
        [
            "show",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD:README.md",
            "--",
            "README.md",
        ]
    ], f"the normal path must forward commit + paths separately; got: {argv!r}"

    assert not result.strip().lower().startswith("error"), (
        f"a ':' in the commit must NOT be refused on the normal path; "
        f"got: {result!r}"
    )
