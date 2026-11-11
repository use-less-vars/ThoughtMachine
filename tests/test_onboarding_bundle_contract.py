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

# Exactly the manifest entries that DO record a digest: the six members whose
# bytes are stable across branches, and therefore the six the digest is a real
# tripwire for. Kept next to ADVISORY_MEMBER so the two cannot drift apart.
HASHED_MEMBERS = [
    "docs/CONTROL_PLANE.md",
    ".thoughtmachine/knowledge/project/MANDATORY_STOP.md",
    ".thoughtmachine/knowledge/project/architecture_invariants.md",
    "docs/ARCHITECTURE_MACHINE_MODEL.md",
    ".thoughtmachine/knowledge/project/vision_ledger.md",
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
PINNED_DEV_SHA = "b43163dc"
PINNED_DEV_SUBJECT = "docs: log security boundary C-items"
PINNED_RECENT = (
    "b43163dc docs: log security boundary C-items\n"
    "10809b2b docs: log security boundary C-items\n"
    "88d387bf Merge pull request #336 from use-less-vars/fix/gen62-tier0-honest-state\n"
    "0ff6ae6c Merge pull request #337 from use-less-vars/fix/gen62-d16-sliced-wait\n"
    "0c907aff test(tier0): assert the pause route's real status in the stale-handle test"
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
    pinned git facts, the five-commit block, the manifest prose and its six
    stable digests, the rules of engagement, the carried traps, the
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
    all seven manifest entries with their prose, the six stable sha256 digests,
    the rules of engagement, the carried traps, the 'do not re-litigate' block
    and the footer.

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

    active = (REPO_ROOT / ADVISORY_MEMBER).read_text(encoding="utf-8").split(
        "## Archived"
    )[0]
    live_open = {cid for cid, _title in gen_handoff._parse_open_items(active)}

    router_ids = set(re.findall(r"^- \*\*(C\d+)\*\* -- ", _queue_block(), re.M))

    assert router_ids, "the queue block names no C-items"
    assert router_ids <= live_open, (
        "HANDOFF.md's queue block names C-items that are no longer open: "
        f"{sorted(router_ids - live_open)}"
    )


def test_manifest_carries_the_three_new_members():
    entries = _manifest_entries()

    for member in NEW_MEMBERS:
        assert member in entries, f"{member} is missing from HANDOFF.md section 2"


def test_every_recorded_hash_matches_the_file_on_disk():
    """Every entry that carries a digest must match its bytes -- and only the
    six stable members carry one (the queue is ADVISORY, see ADVISORY_MEMBER).
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
