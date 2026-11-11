"""Contract tests for the checked-in onboarding artifacts.

Unlike ``tests/test_onboarding_scripts.py`` (hermetic, synthetic trees), these
run against the REAL repository and only ever read it. They exist because the
router is GENERATED: a hand-edited HANDOFF.md, a manifest that quietly lost one
of its members, a stale recorded digest, or a bundle that drops the router
would each still "look" fine in a diff. Each test pins one of those.

  1. the checked-in HANDOFF.md is byte-identical to a fresh generation from the
     live queue and the live files, with the git-derived header facts pinned
     (see PINNED_* below -- they are the only environment-dependent part);
 1b. those pinned facts are also asserted directly, and the 'last five commits'
     block must start at the same commit the `dev` line names;
  2. section 2 really carries the three members added for C47;
  3. every recorded ``- sha256:`` matches the file it names, on disk;
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
        timeout=120,
        check=False,
    )


def test_checked_in_handoff_matches_fresh_generation(tmp_path):
    """The router is generated: hand-editing it is the drift C47 exists to stop."""
    result = _regenerate(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == HANDOFF.read_text(encoding="utf-8")


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


def test_manifest_carries_the_three_new_members():
    entries = _manifest_entries()

    for member in NEW_MEMBERS:
        assert member in entries, f"{member} is missing from HANDOFF.md section 2"


def test_every_recorded_hash_matches_the_file_on_disk():
    recorded = [
        (DIGEST_RE.search(line).group(1), ENTRY_RE.match(line).group(1))
        for line in _section2_lines()
        if ENTRY_RE.match(line)
    ]

    assert recorded, "section 2 records no sha256 values"
    for digest, rel in recorded:
        assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{rel}: malformed digest"
        actual = hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()
        assert digest == actual, f"{rel} does not match its recorded sha256"


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
