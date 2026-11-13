"""RED test — B2: ``show_file`` must refuse a ``ref`` that carries its own selector.

``tools/git_info_tool.py::GitReadTool._git_show_file`` builds the git argument
``f"{ref}:{rel}"`` — the tool itself supplies the single ``:`` selector that
separates a ref from a path.  When the caller's ``ref`` ALSO contains a ``:``
(e.g. ``ref="HEAD:README.md"``) the constructed argument carries TWO
selectors::

    ref="HEAD:README.md", path="README.md"
        ->  git show --no-ext-diff --no-textconv "HEAD:README.md:README.md"

``GitReadTool._validate_git_ref`` (shared with treeish/object/branch) rejects
leading ``-``, empty strings, leading/trailing whitespace and
whitespace/control characters, but NOT ``:``.  ``ref`` is documented as "a
commit SHA or ref name" and ``_git_show_file``'s docstring as
``git show <ref>:<path>`` — so a ``:`` in ``ref`` is never a legitimate
selector here: it can only produce a malformed, doubled selector.  Such a
``ref`` must be refused LOUDLY.

The tool's git execution seam (``_run_git`` / ``_run_git_raw``) is faked
rather than driven through a real ``git`` binary: this sandbox image has NO
``git`` executable, so a subprocess-driven setup would error for a reason
UNRELATED to the defect.  ``GitReadTool`` is a pydantic model, so the seam
methods are overridden with ``object.__setattr__`` (a plain instance
assignment is rejected by pydantic).

EXPECTED TODAY: RED — the double-selector cases are all ACCEPTED today (no
refusal; the fabricated argument reaches the fake git seam), so the
loud-refusal assertion fails.  The legitimate-ref cases are GREEN today and
pin the no-regression contract.
"""

from pathlib import Path

import pytest

from tools.git_info_tool import GitReadTool


def _tool(tmp_path: Path, argv: list, **overrides) -> GitReadTool:
    """Build a ``show``/``show_file`` tool whose faked git seam records argv.

    ``argv`` collects every argument list handed to ``_run_git`` /
    ``_run_git_raw`` so the test can assert what (if anything) reached git.
    """
    params = {
        "session_id": "s-b2",
        "workspace_id": "ws-b2",
        "session_permissions": {},
    }
    params.update(overrides)
    tool = GitReadTool(**params)

    # pydantic model: bypass validation to install the workspace context and
    # the faked git seam on the instance.
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "ws-b2")
    object.__setattr__(tool, "_use_container_mode", lambda: False)

    def fake_run_git(repo_root, args, timeout=30):
        argv.append(list(args))
        return "fake-out\n"

    def fake_run_git_raw(repo_root, args, timeout=30):
        argv.append(list(args))
        return (0, "fake-out\n", "")

    object.__setattr__(tool, "_run_git", fake_run_git)
    object.__setattr__(tool, "_run_git_raw", fake_run_git_raw)
    return tool


@pytest.mark.parametrize(
    "ref,path",
    [
        ("HEAD:README.md", "README.md"),
        ("HEAD:README.md:extra", "README.md"),
        (":README.md", "README.md"),
        ("v1.0:sub/dir", "file.txt"),
    ],
)
def test_show_file_ref_with_selector_is_refused(tmp_path, ref, path):
    """A ``ref`` carrying its own ``:`` must be refused loudly.

    ``show_file`` builds ``<ref>:<path>`` itself; a ``:`` in ``ref`` therefore
    yields a doubled selector (``HEAD:README.md:README.md``).  It must never
    reach git.
    """
    (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / path).write_text("x\n")

    argv = []
    # ``source="committed"`` is EXPLICIT: this module proves the ``ref``
    # selector contract, which only exists on the committed read.  The tool's
    # default is now the working tree (no git call, no selector built), so an
    # omitted source would never exercise the code under test.
    tool = _tool(
        tmp_path, argv, operation="show_file", ref=ref, path=path,
        source="committed",
    )
    result = tool._git_show_file(tmp_path)

    # LOUD: a human-readable refusal (the exact vocabulary is not pinned here).
    assert result.strip().lower().startswith("error"), (
        f"ref={ref!r} carries its own ':' selector, and show_file builds "
        f"'<ref>:<path>' itself, so this must be refused loudly; got: {result!r}"
    )

    # STRUCTURAL: never hand git an argument carrying a doubled selector.
    assert argv == [], f"expected no git call, got: {argv}"


@pytest.mark.parametrize(
    "ref",
    ["HEAD", "main", "abc1234"],
)
def test_legitimate_refs_unchanged(tmp_path, ref):
    """A plain commit SHA / ref name must produce exactly ``<ref>:<path>``."""
    (tmp_path / "README.md").write_text("x\n")

    argv = []
    # The ref is forwarded only by the committed read; the working-tree default
    # makes no git call at all (see test_legitimate_default_ref_unchanged).
    tool = _tool(
        tmp_path, argv, operation="show_file", ref=ref, path="README.md",
        source="committed",
    )
    tool._git_show_file(tmp_path)

    assert argv == [
        ["show", "--no-ext-diff", "--no-textconv", f"{ref}:README.md"]
    ], f"ref={ref!r} must be forwarded unchanged; got: {argv!r}"


def test_legitimate_default_ref_unchanged(tmp_path):
    """An omitted ``ref`` keeps defaulting to ``HEAD``.

    ``source="committed"`` is EXPLICIT here, and that is the whole point: the
    subject of this test is the DEFAULT REF (``HEAD``) that the committed read
    embeds in its ``<ref>:<path>`` selector.  The test used to reach that read
    through the tool's default source, but the default is now the WORKING TREE
    (which makes no git call), so the source is named out loud instead of being
    relied on incidentally.  What is asserted -- ``HEAD:README.md`` -- has not
    changed.
    """
    (tmp_path / "README.md").write_text("x\n")

    argv = []
    tool = _tool(
        tmp_path, argv, operation="show_file", path="README.md",
        source="committed",
    )
    tool._git_show_file(tmp_path)

    assert argv == [
        ["show", "--no-ext-diff", "--no-textconv", "HEAD:README.md"]
    ], f"the default ref must stay 'HEAD'; got: {argv!r}"
