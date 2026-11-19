"""Contract tests for the checked-in onboarding artifacts.

Unlike ``tests/test_onboarding_scripts.py`` (hermetic, synthetic trees), these
run against the REAL repository and only ever read it. They exist because the
router is GENERATED: a hand-edited HANDOFF.md, a manifest that quietly lost one
of its members, a stale recorded digest, or a bundle that drops the router
would each still "look" fine in a diff. Each test pins one of those.

  1. the checked-in HANDOFF.md is byte-identical to a fresh generation from the
     live files, with the git-derived header facts pinned (see PINNED_* below --
     they are the only environment-dependent part) and the branch-local queue
     block collapsed on both sides before the comparison;
 1b. those pinned facts are also asserted directly, and the 'last five commits'
     block must start at the same commit the `dev` line names;
 1c. the `dev` line's commit is still REACHABLE from `dev` -- the clone is
     deepened as needed first, because a shallow checkout can only report
     "not fetched", which is not the same answer as "not an ancestor";
  2. section 2 really carries the three members added for C47;
  3. every recorded ``- sha256:`` matches the file it names, on disk -- and the
     live work queue records none at all (it is ADVISORY: branch-local bytes);
 3b. the queue section names only C-items that are still open right now;
  4. the bundle leads with the router, verbatim, before the members -- and the
     new members' bytes appear in it unchanged.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DIR_REL = ".thoughtmachine/knowledge/project"
KB_DIR = REPO_ROOT / KB_DIR_REL
HANDOFF = KB_DIR / "HANDOFF.md"
GEN = REPO_ROOT / "scripts" / "gen_handoff.py"
BUILD = REPO_ROOT / "scripts" / "build_onboarding.sh"

# The members C47 added to the manifest (all were missing before it).
NEW_MEMBERS = [
    ".thoughtmachine/knowledge/project/MANDATORY_STOP.md",
    "docs/ARCHITECTURE_MACHINE_MODEL.md",
    ".thoughtmachine/knowledge/project/architecture_invariants.md",
]

# The live work queue: LISTED in section 2, but the one member that records NO
# digest. It is ``ADVISORY`` in scripts/gen_handoff.py (and
# ``KB_ADVISORY_ENTRIES`` on the shell side), because its bytes are branch-local
# -- every branch that opens, closes or re-words a C-item edits it, and every
# merge of dev rewrites it. A recorded digest there would go red on any branch
# that is merely up to date with dev, i.e. on the wrong branches, for the wrong
# reason.
ADVISORY_MEMBER = ".thoughtmachine/knowledge/project/PRIORITIES.md"

# Exactly the manifest entries that DO record a digest, in manifest order: the
# members whose bytes are stable across branches, and therefore the ones the
# digest is a real tripwire for. The list itself is the count -- prose here once
# said "seven" over a list that had grown, which is how a count in a comment
# goes stale -- so no number is repeated in this file. Kept next to
# ADVISORY_MEMBER so the two cannot drift apart.
HASHED_MEMBERS = [
    "docs/CONTROL_PLANE.md",
    ".thoughtmachine/knowledge/project/MANDATORY_STOP.md",
    ".thoughtmachine/knowledge/project/architecture_invariants.md",
    "docs/ARCHITECTURE_MACHINE_MODEL.md",
    ".thoughtmachine/knowledge/project/machine_history_and_vision.md",
    ".thoughtmachine/knowledge/project/vision_ledger.md",
    ".thoughtmachine/knowledge/project/ORCHESTRATOR_GUIDE.md",
    ".thoughtmachine/knowledge/project/CI_PROCESS.md",
    ".thoughtmachine/knowledge/project/INFRA_ONBOARDING.md",
]

QUEUE_HEADING = "## First task (from the live queue)"
# Stands in for the queue body on both sides of the byte-equality comparison.
QUEUE_SENTINEL = "<!-- queue body collapsed: branch-local, see test_queue_block_names_only_items_that_are_still_open -->"

# ── Git-derived facts: PINNED, deliberately ────────────────────────────────
# These three come from git, and git is environment-dependent: `dev` moves, a
# CI runner has a git binary while a sandbox may not, and the five-commit
# window changes with every merge. So they are literals here and are always
# passed to the generator explicitly (--dev-sha/--dev-subject/--recent-file),
# which makes the fresh generation produce the SAME BYTES on a machine with
# git and without it -- the equality test below can never go red merely
# because dev advanced under a pull request.
#
# Everything else the generator emits -- the manifest, the per-entry sha256
# digests, the open C-item list -- is repo CONTENT, not git state, and is
# still regenerated live from the tree, so those stay strict assertions.
#
# Coupling: when dev is meant to move into the router, regenerate HANDOFF.md
# and update these constants in the same change. That is the only link.
PINNED_DEV_SHA = "7bf3f1ab"
PINNED_DEV_SUBJECT = "feat(images): pasted images infrastructure (#412)"
PINNED_RECENT = (
    "7bf3f1ab feat(images): pasted images infrastructure (#412)\n"
    "ddd75159 docs(kb): regenerate HANDOFF.md after #413 edited "
    "INFRA_ONBOARDING.md\n"
    "bbe67b25 docs(ticket): file merge-gate-is-rulesets-not-classic-protection "
    "(C93 follow-up)\n"
    "386f75ad fix(presence): workers are no longer reported dead while answering "
    "(#411)\n"
    "137f2407 docs(kb): correct the stale sizes and the closed C76, and record what "
    "is ruling-blocked (#413)"
)

DEV_LINE_RE = re.compile(r"^- `dev` at `([^`]+)` -- \*(.*)\*$")
ENTRY_RE = re.compile(r"^[0-9]+\.\s+`([^`]+)`")
DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")


def _recorded_dev():
    for line in HANDOFF.read_text(encoding="utf-8").splitlines():
        match = DEV_LINE_RE.match(line)
        if match:
            return match.group(1), match.group(2)
    raise AssertionError("HANDOFF.md has no '- `dev` at `...` -- *...*' line")


def _recent_block():
    """The fenced '## Last five commits on dev' body, without the fences."""
    lines = HANDOFF.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Last five commits on dev")
    fence_open = lines.index("```", start)
    fence_close = lines.index("```", fence_open + 1)
    return "\n".join(lines[fence_open + 1 : fence_close])


def _section2_lines():
    lines = HANDOFF.read_text(encoding="utf-8").splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if re.match(r"^##\s+2\.", line)
    )
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def _manifest_entries():
    return [
        ENTRY_RE.match(line).group(1)
        for line in _section2_lines()
        if ENTRY_RE.match(line)
    ]


def _live_region(text):
    """The part of PRIORITIES.md ABOVE the `## Archived` heading line.

    Cut on the HEADING LINE, not on the first occurrence of the substring
    "## Archived" anywhere in the text. The substring form is what this file used
    to do, and it is the same defect C96 records in the generator: any inline
    mention of the heading inside prose (a backticked `## Archived` in an
    explanation of the slicing bug, for instance) truncated the region EARLY and
    silently, so the live set became whatever happened to precede the mention.
    A heading is a line that starts with it; that is the only form that means
    "the archive starts here".
    """
    match = re.search(r"^## Archived", text, re.M)
    return text if match is None else text[: match.start()]


def _queue_block():
    """The body of the '## First task (from the live queue)' section."""
    lines = HANDOFF.read_text(encoding="utf-8").splitlines()
    start = lines.index(QUEUE_HEADING)
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def _without_queue_block(text):
    """Replace the queue section's body with QUEUE_SENTINEL, keeping the rest.

    The queue body names the C-items that are OPEN in the tree the router was
    generated from. That set is branch-local, not repo-static: a branch that
    opens a C-item adds a bullet, and every merge of dev can add or drop one.
    Comparing it byte for byte would make this test fail on a router that is
    perfectly correct for its own tree (it is regenerated, so it would also
    demand a regeneration on every unrelated merge). Collapsing it to a
    sentinel on both sides keeps every OTHER byte pinned -- the header, the
    pinned git facts, the five-commit block, the manifest prose and its stable
    digests, the rules of engagement, the carried traps, the
    'do not re-litigate' block and the footer -- which is what this test is
    for. The queue body still has its own, weaker contract:
    test_queue_block_names_only_items_that_are_still_open.
    """
    out = []
    in_queue = False
    for line in text.splitlines(keepends=True):
        heading = line.rstrip("\n")
        if heading == QUEUE_HEADING:
            in_queue = True
            out.append(line)
            continue
        if in_queue and heading.startswith("## "):
            in_queue = False
            out.append(QUEUE_SENTINEL + "\n")
            out.append(line)
            continue
        if in_queue:
            continue
        out.append(line)
    if in_queue:
        out.append(QUEUE_SENTINEL + "\n")
    return "".join(out)


def _regenerate(tmp_path):
    """Run the generator with the PINNED git facts, returning the CompletedProcess.

    Pinning is what makes this comparable across environments: with
    --dev-sha/--dev-subject/--recent-file supplied, main() never shells out to
    git, so the output depends only on the tree's content.
    """
    recent_file = tmp_path / "recent.txt"
    recent_file.write_text(PINNED_RECENT + "\n", encoding="utf-8")

    return subprocess.run(
        [
            sys.executable,
            str(GEN),
            "--root",
            str(REPO_ROOT),
            "--dev-sha",
            PINNED_DEV_SHA,
            "--dev-subject",
            PINNED_DEV_SUBJECT,
            "--recent-file",
            str(recent_file),
            "--stdout",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_checked_in_handoff_matches_fresh_generation(tmp_path):
    """The router is generated: hand-editing it is the drift C47 exists to stop.

    Still BYTE-PINNED (the queue body is the only thing collapsed first): the
    title and generation banner, the pinned `dev` line, the five-commit block,
    every manifest entry with its prose, every recorded sha256 digest (the
    stable members' -- see HASHED_MEMBERS for which those are), the rules of
    engagement, the carried traps, the 'do not re-litigate' block and the footer.

    Intentionally NOT pinned: the body of '## First task (from the live
    queue)'. It lists the C-items OPEN in this tree, which is branch-local --
    any branch that opens or closes a C-item, and any merge of dev, changes it
    without the router being wrong. Pinning it byte for byte would fail on
    correct routers and push people to hand-edit generated output. It is
    checked instead by test_queue_block_names_only_items_that_are_still_open.
    """
    result = _regenerate(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _without_queue_block(result.stdout) == _without_queue_block(
        HANDOFF.read_text(encoding="utf-8")
    )


def test_router_header_carries_the_pinned_git_facts():
    """Direct, cheap: a half-updated header fails HERE, not as a 78-line diff."""
    assert _recorded_dev() == (PINNED_DEV_SHA, PINNED_DEV_SUBJECT)
    assert _recent_block().strip() == PINNED_RECENT.strip()


def test_router_recent_block_starts_at_the_recorded_dev_head():
    """The bug this catches: `dev` line corrected, five-commit block left stale."""
    lines = _recent_block().splitlines()

    assert len(lines) == 5, f"expected five commits, got {len(lines)}"
    for line in lines:
        assert re.fullmatch(r"[0-9a-f]{7,40} \S.*", line), f"bad line: {line!r}"
    assert lines[0].split(" ", 1)[0] == _recorded_dev()[0]


# ── Dev ancestry: ESTABLISH it, do not assume it ─────────────────────────────
# merge-base can only answer from the objects in the LOCAL clone. A shallow
# checkout (actions/checkout defaults to fetch-depth: 1) usually does not contain
# the pinned commit at all, and then "not an ancestor" is precisely the WRONG
# report: the commit was never fetched, it did not leave dev. The helper below
# makes the question answerable first -- it deepens the clone until the pin is
# present -- and only then asks it. It takes the runner as a parameter, so the
# whole decision flow is testable with no git binary and no network.
_DEEPEN_STEPS = 200
_GIT_TIMEOUT = 120
# Fetch the branch BY NAME. `dev` is not abbreviated, and it is the very ref the
# ancestry question is about, so making it exist is part of establishing the
# fact. `git fetch origin <sha>` cannot do this job: the pinned sha is stored
# abbreviated (7 characters) and a server will not serve a want for an
# abbreviated oid.
_DEV_REFSPEC = "dev:refs/remotes/origin/dev"


def pin_dev_ancestry(sha, *, run=subprocess.run, repo_root=REPO_ROOT, diagnostics=None):
    """Ask git whether ``sha`` is reachable from ``dev``.

    Three answers, deliberately not a bool, because "git could not answer" must
    never be reported as staleness:

      'ancestor'     -- in the local object database and reachable from dev.
      'not_ancestor' -- dev does not contain it; the pin is stale.
      'unresolvable' -- the history needed to answer could not be obtained (no
                        git binary, no network, a server that refuses to
                        deepen). The caller must FAIL on this, not skip it: an
                        unanswerable question is not a passing one.

    Steps, in the order they run:
      1. `rev-parse --is-shallow-repository` -- shallow, or a full clone?
      2. `rev-parse --verify --quiet <sha>^{commit}` -- is the pin even here?
      3. only when the clone is shallow AND the pin is absent, deepen until it
         arrives: `fetch --unshallow origin`, then
         `fetch origin dev:refs/remotes/origin/dev` (the unshallow failed, or it
         left the branch unborn -- see step 4), then
         `fetch --deepen <_DEEPEN_STEPS> origin` (a server that refuses to
         unshallow). Re-probe after each. Still absent -> 'unresolvable'.
      4. `rev-parse --verify --quiet origin/dev`. `--unshallow` inherits the
         remote's CONFIGURED refspec, and actions/checkout gives a pull_request
         checkout a narrow one (+<sha>:refs/remotes/pull/N/merge), so history can
         arrive while `origin/dev` stays unborn. Hence the one-shot safety net:
         fetch `dev` by name once and re-probe. Ref = `origin/dev` if it exists
         now, else `dev` (a plain local clone with no remote-tracking ref).
         Asking merge-base about an unborn local `dev` would answer 128 -- a hard
         'unresolvable' on a branch that is perfectly healthy.
      5. `merge-base --is-ancestor <sha> <ref>` -- rc 0 -> 'ancestor', rc 1 ->
         'not_ancestor', any other rc (bad cwd, dubious ownership) ->
         'unresolvable'.

    ``diagnostics``, when a list is passed, receives one ``"<cmd> -> rc=N"`` line
    per git invocation, so a failure can name the command that left the question
    unanswerable and not just the conclusion.
    """

    def git(*args):
        command = " ".join(("git", *args))
        try:
            result = run(
                ["git", *args],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
                check=False,
            )
        except OSError as exc:  # no git binary, or it could not be spawned
            if diagnostics is not None:
                diagnostics.append(f"{command} -> {type(exc).__name__}: {exc}")
            return None
        if diagnostics is not None:
            diagnostics.append(f"{command} -> rc={result.returncode}")
        return result

    def present(commit):
        """Is ``commit`` in the local object database?"""
        probe = git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
        return probe is not None and probe.returncode == 0

    shallow_probe = git("rev-parse", "--is-shallow-repository")
    shallow = (
        shallow_probe is not None
        and shallow_probe.returncode == 0
        and (shallow_probe.stdout or "").strip() == "true"
    )

    have_pin = present(sha)
    if shallow and not have_pin:
        for fetch in (
            ("fetch", "--unshallow", "origin"),
            ("fetch", "origin", _DEV_REFSPEC),
            ("fetch", "--deepen", str(_DEEPEN_STEPS), "origin"),
        ):
            git(*fetch)
            have_pin = present(sha)
            if have_pin:
                break

    if not have_pin:
        return "unresolvable"

    def has_origin_dev():
        probe = git("rev-parse", "--verify", "--quiet", "origin/dev")
        return probe is not None and probe.returncode == 0

    have_origin_dev = has_origin_dev()
    if not have_origin_dev:
        git("fetch", "origin", _DEV_REFSPEC)
        have_origin_dev = has_origin_dev()
    ref = "origin/dev" if have_origin_dev else "dev"

    result = git("merge-base", "--is-ancestor", sha, ref)
    if result is None:
        return "unresolvable"
    if result.returncode == 0:
        return "ancestor"
    if result.returncode == 1:
        return "not_ancestor"
    return "unresolvable"


@pytest.mark.skipif(
    shutil.which("git") is None,
    reason=(
        "no git binary: SKIPPED, NOT PASSED -- this is the only assertion in the "
        "repo that can notice the router pinning a commit dev no longer contains, "
        "so a skipped run leaves the router's staleness UNCHECKED. The checkout "
        "no longer has to be deep (pin_dev_ancestry deepens a shallow one), but "
        "RUNNING it still needs a git binary and a `dev` to ask about; the "
        "handler itself is covered without either by the "
        "test_pin_dev_ancestry_* tests below."
    ),
)
def test_pinned_dev_sha_is_an_ancestor_of_dev():
    """The staleness detector for the PINNED_* facts -- complementary, not a swap.

    test_checked_in_handoff_matches_fresh_generation proves the router equals a
    fresh generation WITH THE PINNED FACTS SUPPLIED. That is portable (it needs
    no git, which is why it passes in a sandbox) but it is blind to the facts
    themselves having rotted: if PINNED_DEV_SHA names a commit that is no longer
    on dev, the pinned facts and the generated bytes still agree, and every
    other test here stays green while the router advertises a history that has
    been rebased away. That is exactly the drift C95 records.

    So this test asks git the one question the literals cannot answer: is
    PINNED_DEV_SHA still reachable from `dev`? pin_dev_ancestry does the asking
    because a shallow checkout cannot answer it as-is. Only 'not_ancestor' is
    staleness; 'unresolvable' (no git, no network, history unobtainable) FAILS
    here rather than passing or skipping, because an ancestry question that was
    never answered must not be mistaken for a green one. Dev legitimately moving
    AHEAD of the pin is not a failure: the coupling documented above is that dev
    moves into the router deliberately, in a change that also updates these
    constants.
    """
    diagnostics = []
    state = pin_dev_ancestry(
        PINNED_DEV_SHA, repo_root=REPO_ROOT, diagnostics=diagnostics
    )
    trail = "\n  ".join(diagnostics)

    assert state != "unresolvable", (
        f"could not establish whether PINNED_DEV_SHA {PINNED_DEV_SHA} is an "
        "ancestor of `dev`: the history needed to answer was not obtainable, so "
        "this is NEITHER a pass NOR a staleness report. Commands tried:\n  "
        f"{trail}"
    )
    assert state == "ancestor", (
        f"PINNED_DEV_SHA {PINNED_DEV_SHA} is not an ancestor of `dev`: the router "
        "pins a commit that dev no longer contains. Regenerate HANDOFF.md and "
        "update PINNED_DEV_SHA/PINNED_DEV_SUBJECT/PINNED_RECENT in the same change."
        f"\nCommands tried:\n  {trail}"
    )


class _FakeRun:
    """A ``subprocess.run`` stand-in: records argv and answers from a table.

    Keys are the git arguments AFTER the executable; values are a list of
    ``(returncode, stdout)`` pairs consumed in order (the last pair repeats), so
    a command expected to answer differently before and after a fetch can be
    scripted. An unscripted command answers ``(1, "")`` -- and every command the
    helper should not have reached still shows up in ``git_calls()``, which is
    what the assertions compare.
    """

    def __init__(self, answers):
        self._answers = {key: list(value) for key, value in answers.items()}
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        script = self._answers.get(tuple(argv[1:])) or [(1, "")]
        returncode, stdout = script[0] if len(script) == 1 else script.pop(0)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    def git_calls(self):
        return [call[1:] for call in self.calls]


def test_pin_dev_ancestry_deepens_a_shallow_clone_before_asking():
    """Shallow checkout: the pin is fetched FIRST, then the question is asked.

    The order is the assertion. Asking merge-base before deepening is what makes
    a shallow clone report "the commit was never fetched" as "the pin is stale".
    """
    pin = f"{PINNED_DEV_SHA}^{{commit}}"
    fake = _FakeRun(
        {
            ("rev-parse", "--is-shallow-repository"): [(0, "true\n")],
            ("rev-parse", "--verify", "--quiet", pin): [
                (1, ""),
                (0, PINNED_DEV_SHA + "\n"),
            ],
            ("fetch", "--unshallow", "origin"): [(0, "")],
            ("rev-parse", "--verify", "--quiet", "origin/dev"): [(0, "aaaaaaa\n")],
            ("merge-base", "--is-ancestor", PINNED_DEV_SHA, "origin/dev"): [(0, "")],
        }
    )

    assert pin_dev_ancestry(PINNED_DEV_SHA, run=fake) == "ancestor"
    assert fake.git_calls() == [
        ["rev-parse", "--is-shallow-repository"],
        ["rev-parse", "--verify", "--quiet", pin],
        ["fetch", "--unshallow", "origin"],
        ["rev-parse", "--verify", "--quiet", pin],
        ["rev-parse", "--verify", "--quiet", "origin/dev"],
        ["merge-base", "--is-ancestor", PINNED_DEV_SHA, "origin/dev"],
    ]


def test_pin_dev_ancestry_is_unresolvable_when_the_pin_cannot_be_fetched():
    """History unobtainable -> 'unresolvable', and merge-base is NEVER asked.

    This is the shallow-checkout failure that must not be read as a stale pin:
    all three deepening strategies ran and the commit is still absent, so the
    only honest answer is "could not establish" -- and no ancestry claim is made.
    """
    pin = f"{PINNED_DEV_SHA}^{{commit}}"
    fake = _FakeRun({("rev-parse", "--is-shallow-repository"): [(0, "true\n")]})
    diagnostics = []

    state = pin_dev_ancestry(PINNED_DEV_SHA, run=fake, diagnostics=diagnostics)

    assert state == "unresolvable"
    assert fake.git_calls() == [
        ["rev-parse", "--is-shallow-repository"],
        ["rev-parse", "--verify", "--quiet", pin],
        ["fetch", "--unshallow", "origin"],
        ["rev-parse", "--verify", "--quiet", pin],
        ["fetch", "origin", _DEV_REFSPEC],
        ["rev-parse", "--verify", "--quiet", pin],
        ["fetch", "--deepen", str(_DEEPEN_STEPS), "origin"],
        ["rev-parse", "--verify", "--quiet", pin],
    ]
    assert "merge-base" not in [call[0] for call in fake.git_calls()]
    assert any("fetch --deepen" in line for line in diagnostics)


def test_pin_dev_ancestry_reports_a_stale_pin_as_not_ancestor():
    """rc 1 from merge-base is the real failure: dev no longer contains the pin."""
    pin = f"{PINNED_DEV_SHA}^{{commit}}"
    fake = _FakeRun(
        {
            ("rev-parse", "--is-shallow-repository"): [(0, "false\n")],
            ("rev-parse", "--verify", "--quiet", pin): [(0, PINNED_DEV_SHA + "\n")],
            ("rev-parse", "--verify", "--quiet", "origin/dev"): [(0, "bbbbbbb\n")],
            ("merge-base", "--is-ancestor", PINNED_DEV_SHA, "origin/dev"): [(1, "")],
        }
    )

    assert pin_dev_ancestry(PINNED_DEV_SHA, run=fake) == "not_ancestor"
    # A full clone that already has the pin must not pay for a fetch.
    assert "fetch" not in [call[0] for call in fake.git_calls()]


def test_pin_dev_ancestry_falls_back_to_the_local_dev_ref():
    """`origin/dev` still unborn after the safety-net fetch -> ask about `dev`.

    A plain local clone has no remote-tracking ref and a server may refuse the
    refspec; the question is then put to the local `dev` instead of abandoned.
    """
    pin = f"{PINNED_DEV_SHA}^{{commit}}"
    fake = _FakeRun(
        {
            ("rev-parse", "--is-shallow-repository"): [(0, "false\n")],
            ("rev-parse", "--verify", "--quiet", pin): [(0, PINNED_DEV_SHA + "\n")],
            ("rev-parse", "--verify", "--quiet", "origin/dev"): [(1, "")],
            ("merge-base", "--is-ancestor", PINNED_DEV_SHA, "dev"): [(0, "")],
        }
    )

    assert pin_dev_ancestry(PINNED_DEV_SHA, run=fake) == "ancestor"
    assert fake.git_calls() == [
        ["rev-parse", "--is-shallow-repository"],
        ["rev-parse", "--verify", "--quiet", pin],
        ["rev-parse", "--verify", "--quiet", "origin/dev"],
        ["fetch", "origin", _DEV_REFSPEC],
        ["rev-parse", "--verify", "--quiet", "origin/dev"],
        ["merge-base", "--is-ancestor", PINNED_DEV_SHA, "dev"],
    ]


def test_pin_dev_ancestry_fetches_dev_when_the_pr_checkout_lacks_it():
    """PR checkout: history arrived but `origin/dev` did not -> fetch the ref ONCE.

    actions/checkout gives a pull_request checkout a narrow refspec, so
    `--unshallow` can bring the pinned commit while leaving `origin/dev` unborn.
    Asking merge-base about a local `dev` that a PR checkout does not have would
    answer 128 and turn a healthy branch red, so the ref is fetched by name first
    -- exactly one fetch -- and the question is then asked of the ref that exists.
    """
    pin = f"{PINNED_DEV_SHA}^{{commit}}"
    fake = _FakeRun(
        {
            ("rev-parse", "--is-shallow-repository"): [(0, "false\n")],
            ("rev-parse", "--verify", "--quiet", pin): [(0, PINNED_DEV_SHA + "\n")],
            ("rev-parse", "--verify", "--quiet", "origin/dev"): [
                (1, ""),
                (0, "ccccccc\n"),
            ],
            ("fetch", "origin", _DEV_REFSPEC): [(0, "")],
            ("merge-base", "--is-ancestor", PINNED_DEV_SHA, "origin/dev"): [(0, "")],
        }
    )

    assert pin_dev_ancestry(PINNED_DEV_SHA, run=fake) == "ancestor"
    assert fake.git_calls() == [
        ["rev-parse", "--is-shallow-repository"],
        ["rev-parse", "--verify", "--quiet", pin],
        ["rev-parse", "--verify", "--quiet", "origin/dev"],
        ["fetch", "origin", _DEV_REFSPEC],
        ["rev-parse", "--verify", "--quiet", "origin/dev"],
        ["merge-base", "--is-ancestor", PINNED_DEV_SHA, "origin/dev"],
    ]


def test_priorities_carries_no_recorded_digest():
    """The live queue must stay LISTED but must record NO digest.

    PRIORITIES.md is edited by every branch that opens, closes or re-words a
    C-item and rewritten whenever dev is merged, so a digest recorded here is
    branch-local by definition. Hard-recording it would make this very test
    suite red on any branch that is merely up to date with dev -- and the
    obvious 'fix' for that false alarm is hand-editing the router, which is the
    drift the generated HANDOFF exists to stop. Advisory (ADVISORY in
    scripts/gen_handoff.py) means: still listed, because it is the first thing
    to read, just never hashed.
    """
    entries = [
        line
        for line in _section2_lines()
        if ENTRY_RE.match(line) and ENTRY_RE.match(line).group(1) == ADVISORY_MEMBER
    ]

    assert entries, f"section 2 no longer lists {ADVISORY_MEMBER}"
    assert len(entries) == 1, f"{ADVISORY_MEMBER} listed {len(entries)} times"

    line = entries[0]
    assert "sha256" not in line, f"advisory entry still records a digest: {line!r}"
    assert DIGEST_RE.search(line) is None, line
    assert ADVISORY_MEMBER not in HASHED_MEMBERS


def test_queue_block_names_only_items_that_are_still_open():
    """The queue body is branch-local, so it is pinned as a SUBSET, not bytes.

    The router's '## First task (from the live queue)' section is generated from
    the OPEN C-items of PRIORITIES.md, which differ on every branch. What must
    nevertheless hold on every branch: every bullet it names is a C-item that
    is still open in the tree the router ships with. A bullet for a closed item
    (or for an id that does not exist) is a stale router, and that IS this
    test's failure.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import gen_handoff

    live_open = {
        cid
        for cid, _title in gen_handoff._parse_open_items(
            _live_region((REPO_ROOT / ADVISORY_MEMBER).read_text(encoding="utf-8"))
        )
    }

    router_ids = set(re.findall(r"^- \*\*(C\d+)\*\* -- ", _queue_block(), re.M))

    assert router_ids, "the queue block names no C-items"
    assert router_ids <= live_open, (
        "HANDOFF.md's queue block names C-items that are no longer open: "
        f"{sorted(router_ids - live_open)}"
    )


def test_manifest_carries_the_c47_members():
    entries = _manifest_entries()

    for member in NEW_MEMBERS:
        assert member in entries, f"{member} is missing from HANDOFF.md section 2"


def test_every_recorded_hash_matches_the_file_on_disk():
    """Every entry that carries a digest must match its bytes -- and exactly the
    stable members carry one (HASHED_MEMBERS is the list; the queue is ADVISORY,
    see ADVISORY_MEMBER).
    """
    recorded = []
    for line in _section2_lines():
        entry = ENTRY_RE.match(line)
        if not entry:
            continue
        digest = DIGEST_RE.search(line)
        if digest is None:
            # Only an advisory entry may reach here; test_priorities_carries_no_
            # recorded_digest pins WHICH entry that is.
            continue
        recorded.append((digest.group(1), entry.group(1)))

    assert recorded, "section 2 records no sha256 values"
    for digest, rel in recorded:
        assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{rel}: malformed digest"
        actual = hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
        assert digest == actual, f"{rel} does not match its recorded sha256"

    # The digests must cover exactly the stable members: an entry that quietly
    # gained or lost a token would otherwise pass this loop unnoticed.
    assert {rel for _digest, rel in recorded} == set(HASHED_MEMBERS)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_bundle_carries_router_first_and_members_verbatim(tmp_path):
    bundle = tmp_path / "bundle.md"
    env = dict(os.environ)
    env.pop("KB_REPO_ROOT", None)
    env["HANDOFF_FILE"] = str(HANDOFF)
    env["ONBOARDING_BUNDLE"] = str(bundle)

    result = subprocess.run(
        [shutil.which("bash"), str(BUILD)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    text = bundle.read_text(encoding="utf-8")

    markers = [line for line in text.splitlines() if line.startswith("### File: ")]
    assert markers[0] == f"### File: {KB_DIR_REL}/HANDOFF.md ###"
    assert markers == [f"### File: {KB_DIR_REL}/HANDOFF.md ###"] + [
        f"### File: {entry} ###" for entry in _manifest_entries()
    ]

    # Verbatim: the router and the new members appear byte for byte.
    assert HANDOFF.read_text(encoding="utf-8") in text
    for member in NEW_MEMBERS:
        assert (REPO_ROOT / member).read_text(encoding="utf-8") in text, member
