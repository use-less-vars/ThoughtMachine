"""READ verb contracts: ``show_file`` source, ``log`` ref scope, ``diff`` lists.

Hermetic by necessity: this sandbox image has NO ``git`` executable, so every
test fakes the tool's git seam (``_run_git`` / ``_run_git_raw``) and records the
argv that would have been executed.  That is the SAME scaffolding the pinned
modules ``tests/test_git_show_double_selector.py`` and
``tests/test_git_show_range_double_selector.py`` use (``GitReadTool`` is a
pydantic model, so the seam is installed with ``object.__setattr__`` rather
than a plain assignment).

What is pinned here:

* ``show_file(source="working")`` returns the bytes ON DISK, not the blob at
  HEAD -- a stale-blob read that reads like a working-tree read is how
  unreviewed lines get committed;
* the result NAMES the source it used, and the ref when it read history;
* ``source="committed"`` returns exactly the HEAD blob;
* the line window is identical for both sources;
* ``diff`` with a LIST of paths asks git once for the union (every path after a
  single ``--``) and surfaces hunks for both;
* ``log`` scopes to ``ref=`` (it used to be ignored there);
* an EMPTY diff says WHY it is empty instead of returning a silent blank.
"""

from pathlib import Path

import pytest

from tools.git_info_tool import GitReadTool
from tools.git_write_tool import GitWriteTool

#: Every git tool result ends with these two diagnostics lines (see the
#: ``GitReadTool`` docstring).  They are real output; the helpers below strip
#: them before comparing BODY content, they do not weaken any assertion.
_TRAILER_PREFIXES = ("execution_mode:", "failure_reason:")


def _body(out: str) -> str:
    return "\n".join(
        line for line in out.splitlines()
        if not line.startswith(_TRAILER_PREFIXES)
    )


def _tool(tmp_path: Path, argv: list, raw=(0, "", ""), git_out: str = "", **overrides):
    """Build a ``GitReadTool`` whose faked git seam records argv.

    ``argv`` collects every argument list handed to ``_run_git`` (string
    return) or ``_run_git_raw`` (``(exit_code, stdout, stderr)`` return), so a
    test can assert exactly what would have reached git.
    """
    params = {
        "session_id": "s-verbs",
        "workspace_id": "ws-verbs",
        "session_permissions": {},
    }
    params.update(overrides)
    tool = GitReadTool(**params)

    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "ws-verbs")
    object.__setattr__(tool, "_use_container_mode", lambda: False)
    # Keep the tool's own path validation in play, minus the workspace registry
    # lookup: the contract under test is that paths resolve RELATIVE TO THE
    # REPO ROOT for both sources.
    object.__setattr__(tool, "_validate_path", lambda p: str(p))

    def fake_run_git(repo_root, args, timeout=30):
        argv.append(list(args))
        return git_out

    def fake_run_git_raw(repo_root, args, timeout=30):
        argv.append(list(args))
        return raw

    object.__setattr__(tool, "_run_git", fake_run_git)
    object.__setattr__(tool, "_run_git_raw", fake_run_git_raw)
    return tool


# ---------------------------------------------------------------------------
# WRITE verbs (branch_create base, checkout state, stash scope, stash_drop).
# Same fake-seam style as above; ``GitWriteTool`` additionally needs the
# effective git/filesystem grant, otherwise every verb returns FLAG_ERROR.
# ---------------------------------------------------------------------------


def _write_tool(tmp_path: Path, argv: list, raw=(0, "", ""), git_out: str = "", **overrides):
    """Build a ``GitWriteTool`` whose faked git seam records argv."""
    params = {
        "session_id": "s-verbs",
        "workspace_id": "ws-verbs",
        "session_permissions": {},
    }
    params.update(overrides)
    tool = GitWriteTool(**params)

    object.__setattr__(tool, "effective_permissions", {"git": "write", "filesystem": "write"})
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "ws-verbs")
    object.__setattr__(tool, "_use_container_mode", lambda: False)
    object.__setattr__(tool, "_validate_path", lambda p: str(p))

    def fake_run_git(repo_root, args, timeout=30):
        argv.append(list(args))
        return git_out

    def fake_run_git_raw(repo_root, args, timeout=30):
        argv.append(list(args))
        return raw

    object.__setattr__(tool, "_run_git", fake_run_git)
    object.__setattr__(tool, "_run_git_raw", fake_run_git_raw)
    return tool


def test_branch_create_base_dev_is_resolved_not_head(tmp_path):
    """``base="dev"`` resolves DEV -- no HEAD probe, no fallback to HEAD."""
    argv: list = []
    tool = _write_tool(
        tmp_path, argv, raw=(0, "deadbeef\n", ""),
        operation="branch_create", branch="feat/x", base="dev",
    )

    tool._git_branch_create(tmp_path)

    assert ["rev-parse", "--verify", "dev^{commit}"] in argv, argv
    flat = [item for call in argv for item in call]
    assert "HEAD" not in flat, f"an explicit base must never probe HEAD: {argv}"
    assert "HEAD^{commit}" not in flat, argv
    assert argv[-1] == ["branch", "feat/x", "deadbeef"], argv


def test_branch_create_never_emits_checkout_or_switch(tmp_path):
    """Create-only: no checkout/switch verb may appear anywhere in the argv."""
    argv: list = []
    tool = _write_tool(
        tmp_path, argv, raw=(0, "cafe1234\n", ""),
        operation="branch_create", branch="feat/x", base="dev",
    )

    tool._git_branch_create(tmp_path)

    verbs = [call[0] for call in argv if call]
    assert "checkout" not in verbs and "switch" not in verbs, argv
    assert verbs == ["rev-parse", "branch"], argv


def test_branch_create_reports_base_sha_and_untouched_tree(tmp_path):
    """The body names the resolved base SHA and says the tree was not moved."""
    body = _body(
        _write_tool(
            tmp_path, [], raw=(0, "cafe1234\n", ""),
            operation="branch_create", branch="feat/x", base="dev",
        )._git_branch_create(tmp_path)
    )

    assert "feat/x" in body and "dev" in body and "cafe1234" in body, body
    assert "HEAD not moved" in body, body


def test_branch_create_unresolvable_base_names_the_ref(tmp_path):
    """A base that does not resolve fails LOUDLY and names it (no fallback)."""
    result = _write_tool(
        tmp_path, [], raw=(128, "", "fatal: Needed a single revision\n"),
        operation="branch_create", branch="feat/x", base="nope",
    )._git_branch_create(tmp_path)

    assert result.startswith("Git command failed (exit code 128):"), result
    assert "'nope'" in result, result
    assert "no fallback" in result, result


def test_checkout_reports_previous_ref_new_branch_and_sha(tmp_path):
    """A clean switch prints nothing in git -- the body must still say what moved."""
    argv: list = []
    tool = _write_tool(tmp_path, argv, operation="checkout", branch="feature/x")

    def fake_run_git_raw(repo_root, args, timeout=30):
        argv.append(list(args))
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, "main\n", "")
        if args[:2] == ["rev-parse", "--verify"]:
            return (0, "cafe1234\n", "")
        return (0, "", "")

    object.__setattr__(tool, "_run_git_raw", fake_run_git_raw)
    out = tool._git_checkout(tmp_path)
    body = _body(out)

    assert body.strip(), f"checkout must never return an empty body: {out!r}"
    assert "feature/x" in body and "cafe1234" in body, body
    assert "main" in body, body
    # the switch is the LAST git command issued, and it is the only checkout
    assert argv[-1] == ["checkout", "feature/x"], argv
    assert [c for c in argv if c[:1] == ["checkout"]] == [["checkout", "feature/x"]]


def test_checkout_says_so_when_new_state_is_unknown(tmp_path):
    """If the target SHA cannot be resolved the body says so, not a bare name."""
    def fake_run_git_raw(repo_root, args, timeout=30):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, "main\n", "")
        return (128, "", "fatal: not a valid object name\n")

    tool = _write_tool(tmp_path, [], operation="checkout", branch="feature/x")
    object.__setattr__(tool, "_run_git_raw", fake_run_git_raw)
    body = _body(tool._git_checkout(tmp_path))

    assert "could not be determined" in body, body
    assert "feature/x" in body, body


def test_stash_push_file_path_scopes_the_argv(tmp_path):
    """``file_path`` is honoured as the stash path scope (it used to be IGNORED)."""
    argv: list = []
    tool = _write_tool(
        tmp_path, argv, raw=(0, "Saved working directory\n", ""),
        operation="stash_push", message="wip", file_path=["a.py", "b.py"],
    )

    tool._git_stash_push(tmp_path)

    assert argv == [["stash", "push", "-m", "wip", "--", "a.py", "b.py"]], argv
    flat = [item for call in argv for item in call]
    assert "-a" not in flat and "-u" not in flat, argv


def test_stash_push_paths_still_win_over_file_path(tmp_path):
    """When both are given, the stash-specific ``paths`` list is the scope."""
    argv: list = []
    tool = _write_tool(
        tmp_path, argv, raw=(0, "", ""),
        operation="stash_push", message="wip", paths=["p.py"], file_path=["f.py"],
    )

    tool._git_stash_push(tmp_path)

    assert argv == [["stash", "push", "-m", "wip", "--", "p.py"]], argv


def test_stash_drop_validates_index(tmp_path):
    """Same index contract as stash_pop; a valid index drops stash@{N}."""
    argv: list = []
    tool = _write_tool(tmp_path, argv, operation="stash_drop", index=-1)

    assert tool._git_stash_drop(tmp_path) == (
        "Error: index must be a non-negative integer for stash_drop"
    )
    assert argv == []

    object.__setattr__(tool, "index", True)
    assert tool._git_stash_drop(tmp_path) == (
        "Error: index must be a non-negative integer for stash_drop"
    )
    assert argv == []

    valid = _write_tool(
        tmp_path, argv, raw=(0, "Dropped refs/stash@{2}\n", ""),
        operation="stash_drop", index=2,
    )
    out = valid._git_stash_drop(tmp_path)

    assert argv == [["stash", "drop", "stash@{2}"]], argv
    assert out.startswith("Dropped refs/stash@{2}"), out
    assert "execution_mode" in out, out


def test_stash_drop_missing_index_names_the_valid_range(tmp_path):
    """An absent stash index explains the range instead of a bare git error."""
    def fake_run_git_raw(repo_root, args, timeout=30):
        if args[:2] == ["stash", "list"]:
            return (0, "stash@{0}: WIP on main\nstash@{1}: other\n", "")
        return (1, "", "error: stash@{5} is not a valid reference\n")

    tool = _write_tool(tmp_path, [], operation="stash_drop", index=5)
    object.__setattr__(tool, "_run_git_raw", fake_run_git_raw)
    body = _body(tool._git_stash_drop(tmp_path))

    assert "5" in body and "does not exist" in body, body
    assert "0..1" in body, body


# ---------------------------------------------------------------------------
# show_file: the source is no longer a lie
# ---------------------------------------------------------------------------

def test_show_file_working_returns_disk_bytes_not_head_blob(tmp_path):
    """``source="working"`` reads the file as it is NOW, never the blob."""
    (tmp_path / "a.txt").write_text("WORKING-EDIT\nsecond line\n")
    argv: list = []
    tool = _tool(
        tmp_path,
        argv,
        raw=(0, "HEAD-BLOB\nold line\n", ""),
        operation="show_file",
        path="a.txt",
        source="working",
    )

    out = tool._git_show_file(tmp_path)

    assert "WORKING-EDIT" in out, f"the uncommitted edit must be visible: {out!r}"
    assert "HEAD-BLOB" not in out, (
        f"source='working' must not return the committed blob: {out!r}"
    )
    assert argv == [], f"a working-tree read must not consult git at all: {argv!r}"


def test_show_file_names_the_source_it_used(tmp_path):
    """A reader must never have to guess whether it saw disk or history."""
    (tmp_path / "a.txt").write_text("disk\n")
    argv: list = []

    working = _tool(
        tmp_path, argv, operation="show_file", path="a.txt", source="working"
    )._git_show_file(tmp_path)
    assert working.splitlines()[0].startswith("source: working"), (
        f"the working read must name its source on the first line: {working!r}"
    )

    committed = _tool(
        tmp_path,
        argv,
        raw=(0, "blob\n", ""),
        operation="show_file",
        path="a.txt",
        source="committed",
    )._git_show_file(tmp_path)
    first = committed.splitlines()[0]
    assert first.startswith("source: committed"), (
        f"the committed read must name its source on the first line: {committed!r}"
    )
    assert "ref=HEAD" in first, f"the committed read must name the ref read: {first!r}"


def test_show_file_committed_matches_head_blob(tmp_path):
    """``source="committed"`` is exactly ``git show <ref>:<path>``."""
    (tmp_path / "a.txt").write_text("UNCOMMITTED\n")
    argv: list = []
    tool = _tool(
        tmp_path,
        argv,
        raw=(0, "AT-HEAD\n", ""),
        operation="show_file",
        path="a.txt",
        source="committed",
    )

    out = tool._git_show_file(tmp_path)

    assert argv == [["show", "--no-ext-diff", "--no-textconv", "HEAD:a.txt"]], (
        f"the committed read must be a single <ref>:<path> show: {argv!r}"
    )
    assert "AT-HEAD" in out, f"the HEAD blob must be returned: {out!r}"
    assert "UNCOMMITTED" not in out, (
        f"the committed read must not leak working-tree bytes: {out!r}"
    )


def test_show_file_line_window_is_identical_for_both_sources(tmp_path):
    """``line_start``/``line_end`` must mean the same thing for both sources."""
    content = "\n".join(f"l{i}" for i in range(1, 11)) + "\n"
    (tmp_path / "a.txt").write_text(content)
    argv: list = []
    window = {"operation": "show_file", "path": "a.txt", "line_start": 3, "line_end": 5}

    working = _body(
        _tool(tmp_path, argv, source="working", **window)._git_show_file(tmp_path)
    )
    committed = _body(
        _tool(tmp_path, argv, raw=(0, content, ""), source="committed", **window)
        ._git_show_file(tmp_path)
    )

    # The only permitted difference is the naming line.
    def _sans_source(body: str) -> list:
        return [line for line in body.splitlines() if not line.startswith("source: ")]

    assert _sans_source(working) == ["l3", "l4", "l5"], working
    assert _sans_source(committed) == ["l3", "l4", "l5"], committed


# ---------------------------------------------------------------------------
# diff: a LIST of paths is the union, and an empty result explains itself
# ---------------------------------------------------------------------------

def test_diff_path_list_returns_hunks_for_every_named_path(tmp_path):
    """A list must be ONE ``diff -- p1 p2`` call that returns both hunks."""
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("x\n")
    argv: list = []
    hunks = {
        "a.txt": "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old-a\n+new-a\n",
        "b.txt": "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-old-b\n+new-b\n",
    }

    def fake(repo_root, args, timeout=30):
        argv.append(list(args))
        # git renders ONE stream carrying a hunk per changed path.
        return "".join(hunk for path, hunk in hunks.items() if path in args)

    tool = _tool(tmp_path, argv, operation="diff", file_path=["a.txt", "b.txt"])
    object.__setattr__(tool, "_run_git", fake)

    out = tool._git_diff(tmp_path)

    assert argv == [
        ["diff", "--no-ext-diff", "--no-textconv", "--", "a.txt", "b.txt"]
    ], f"a path list must be a single union invocation: {argv!r}"
    assert "new-a" in out and "new-b" in out, (
        f"hunks for BOTH named paths must be returned: {out!r}"
    )


@pytest.mark.parametrize("paths", [None, ["a.txt"]])
def test_empty_diff_says_why(tmp_path, paths):
    """Silent empty output is the failure mode; the two cases must differ."""
    (tmp_path / "a.txt").write_text("x\n")
    argv: list = []
    kwargs = {"operation": "diff"}
    if paths is not None:
        kwargs["file_path"] = paths

    tool = _tool(tmp_path, argv, git_out="", **kwargs)
    out = tool._git_diff(tmp_path)

    assert "no diff output" in out, f"an empty diff must explain itself: {out!r}"
    if paths is None:
        assert "No changes" in out, f"whole-tree empty means nothing changed: {out!r}"
    else:
        assert "Nothing matched" in out and "a.txt" in out, (
            f"a scoped empty diff must say nothing matched the named path: {out!r}"
        )


# ---------------------------------------------------------------------------
# log: ref= must scope the log
# ---------------------------------------------------------------------------

def test_log_ref_scopes_the_log(tmp_path):
    """``ref=`` used to be ignored here; it now selects the revision."""
    argv: list = []
    _tool(tmp_path, argv, operation="log", ref="release-1.0")._git_log(tmp_path)

    assert argv and argv[0][0] == "log", argv
    assert argv[0][-1] == "release-1.0", (
        f"ref= must reach git as the log revision: {argv!r}"
    )


def test_log_without_ref_stays_head(tmp_path):
    """An omitted ``ref`` must not change the argv (HEAD is git's default)."""
    argv: list = []
    _tool(tmp_path, argv, operation="log")._git_log(tmp_path)

    assert argv == [
        ["log", "--no-ext-diff", "--no-textconv", "--max-count=50", "--oneline"]
    ], f"an omitted ref must leave the log argv untouched: {argv!r}"


# ---------------------------------------------------------------------------
# show_file: the DEFAULT is the working tree, and a missing file tells the truth
# ---------------------------------------------------------------------------

def test_show_file_omitted_source_reads_the_working_tree(tmp_path):
    """An OMITTED ``source`` must read the file as it is now, and say so.

    The default used to be the committed blob, silently -- so a caller could be
    handed history while believing it had read the working tree.  It is now the
    working tree, named on the first line, and no git call is made at all.
    """
    (tmp_path / "a.txt").write_text("UNCOMMITTED\n", encoding="utf-8")
    argv: list = []
    tool = _tool(
        tmp_path,
        argv,
        raw=(0, "AT-HEAD\n", ""),
        operation="show_file",
        path="a.txt",
    )

    out = tool._git_show_file(tmp_path)

    assert argv == [], f"the default read must not consult git: {argv!r}"
    assert out.splitlines()[0].startswith("source: working"), (
        f"the default read must name its source on the first line: {out!r}"
    )
    assert "UNCOMMITTED" in out, f"the uncommitted edit must be visible: {out!r}"
    assert "AT-HEAD" not in out, (
        f"the default must not return the committed blob: {out!r}"
    )


def _probe_tool(tmp_path, argv: list, probe_exit: int):
    """A ``show_file`` tool whose single ``cat-file -e`` probe reports ``exit``.

    ``_run_git_raw`` is the tool's EXISTING backend seam, so the existence probe
    is exercised through the same plumbing (and the same validation) as a real
    read -- nothing new is introduced for it.
    """
    tool = _tool(tmp_path, argv, operation="show_file", path="gone.txt")

    def fake_run_git_raw(repo_root, args, timeout=30):
        argv.append(list(args))
        assert args[:2] == ["cat-file", "-e"], args
        return (probe_exit, "", "")

    object.__setattr__(tool, "_run_git_raw", fake_run_git_raw)
    return tool


def test_show_file_missing_on_disk_but_present_at_head_says_so(tmp_path):
    """A missing working-tree file must NOT hide a committed version.

    The old failure said only "does not exist on disk"; the caller had no way
    to learn that the very same path IS committed.  It now probes once and says
    exactly what to pass next.
    """
    argv: list = []
    out = _probe_tool(tmp_path, argv, 0)._git_show_file(tmp_path)

    assert argv == [["cat-file", "-e", "HEAD:gone.txt"]], (
        f"one existence probe, against the default ref: {argv!r}"
    )
    assert out.startswith("Error:"), out
    assert "absent from the working tree" in out, out
    assert "DOES exist in HEAD" in out, (
        f"a present committed version must be named: {out!r}"
    )
    assert "source='committed'" in out, (
        f"the caller must be told which source to ask for: {out!r}"
    )


def test_show_file_missing_everywhere_says_there_is_no_fallback(tmp_path):
    """A miss in BOTH trees must say there is nothing to fall back to."""
    argv: list = []
    out = _probe_tool(tmp_path, argv, 1)._git_show_file(tmp_path)

    assert argv == [["cat-file", "-e", "HEAD:gone.txt"]], (
        f"one existence probe, against the default ref: {argv!r}"
    )
    assert out.startswith("Error:"), out
    assert "absent from the working tree" in out, out
    assert "does NOT exist in HEAD either" in out, (
        f"an absent committed version must be stated, not guessed: {out!r}"
    )


# ---------------------------------------------------------------------------
# show_file: a binary / non-UTF-8 file is REFUSED, not shown as mojibake
# ---------------------------------------------------------------------------

def test_show_file_working_refuses_a_binary_file(tmp_path):
    """A NUL byte means this is not a text file: refuse, do not show it."""
    (tmp_path / "bin.dat").write_bytes(b"PK\x03\x04\x00\x00payload")
    argv: list = []

    out = _tool(tmp_path, argv, operation="show_file", path="bin.dat")._git_show_file(
        tmp_path
    )

    assert argv == [], f"a refused read must not consult git: {argv!r}"
    assert out.startswith("Error:"), out
    assert "contains a NUL byte at offset 4" in out, out
    assert "text reader" in out, (
        f"the refusal must say show_file is a text reader: {out!r}"
    )
    assert "payload" not in out, f"the bytes must not be leaked: {out!r}"


def test_show_file_working_refuses_non_utf8_text(tmp_path):
    """Replacement characters used to turn latin-1 into plausible-looking text."""
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9 au lait\n")
    argv: list = []

    out = _tool(
        tmp_path, argv, operation="show_file", path="latin1.txt"
    )._git_show_file(tmp_path)

    assert out.startswith("Error:"), out
    assert "not UTF-8 text" in out, out
    assert "0xe9 at offset 3" in out, out
    assert "caf" not in out, f"no bytes may be echoed back: {out!r}"


def test_show_file_working_keeps_valid_multibyte_utf8(tmp_path):
    """Strict decoding must not refuse ordinary UTF-8."""
    (tmp_path / "u.txt").write_text("caf\u00e9 \u2615\n", encoding="utf-8")
    argv: list = []

    out = _tool(tmp_path, argv, operation="show_file", path="u.txt")._git_show_file(
        tmp_path
    )

    assert "caf\u00e9 \u2615" in out, out
    assert "Error:" not in out, out


def test_show_file_working_bound_tolerates_a_char_cut_by_the_bound(
    tmp_path, monkeypatch
):
    """The read bound may slice a multi-byte character in half; that is NOT a
    non-UTF-8 file, and the header must say the read was a prefix."""
    import tools.git_info_tool as git_info_tool

    monkeypatch.setattr(git_info_tool, "_SHOW_FILE_MAX_READ_BYTES", 8)
    (tmp_path / "cut.txt").write_bytes(b"abcdefg\xc3\xa9")
    argv: list = []

    out = _tool(tmp_path, argv, operation="show_file", path="cut.txt")._git_show_file(
        tmp_path
    )

    assert "abcdefg" in out, out
    assert "not UTF-8" not in out, (
        f"a character cut by the bound is not a decoding failure: {out!r}"
    )
    assert "first 8 bytes only" in out, (
        f"a bounded read must declare itself: {out!r}"
    )

