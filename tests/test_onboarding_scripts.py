"""Hermetic contract tests for the KB onboarding scripts.

Covers ``scripts/build_onboarding.sh`` and ``scripts/check_kb_staleness.sh``
against a synthetic project tree under ``tmp_path`` -- the real repository's
HANDOFF.md and the real file mtimes are never consulted, so these tests are
deterministic on any checkout (including today's *template* router, whose
``<placeholder>`` text is irrelevant here).

The contract exercised:

  * the manifest is the numbered, first-token-backticked items of the router's
    section 2 (``## 2. <title>``; the title itself is deliberately not matched
    because humans rewrite it);
  * entries resolve next to the router first, then at the repo root;
  * the bundle preserves manifest order and reports its own byte size;
  * an absent section, an empty manifest, and a missing referenced file are
    hard errors -- never a silently empty or partial bundle;
  * staleness: strictly-newer referenced files and missing ones both WARN and
    exit 1 (deliberate: the operator intends to gate CI on this).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_onboarding.sh"
STALENESS_SCRIPT = REPO_ROOT / "scripts" / "check_kb_staleness.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)

BASH = shutil.which("bash")

# Fixed epoch: no test may depend on the wall clock. Offsets are in HOURS so the
# arithmetic stays unambiguous on every filesystem timestamp granularity.
EPOCH = 1_600_000_000

KB_DIR_REL = ".thoughtmachine/knowledge/project"

# (path relative to the router's directory, body). The fourth body deliberately
# has no trailing newline so the bundle's newline repair is exercised.
MANIFEST = [
    ("PRIORITIES.md", "PRIORITIES BODY\nsecond line\n"),
    ("vision_ledger.md", "VISION BODY\n"),
    ("INFRA_ONBOARDING.md", "INFRA BODY"),
    ("status.md", "STATUS BODY\n"),
]


def _make_project(
    tmp_path, entries=None, manifest_heading="## 2. Read in this order", root_only=()
):
    """Build a fake repo + router; returns (repo, kb_dir, handoff_path)."""
    entries = MANIFEST if entries is None else entries

    repo = tmp_path / "repo"
    kb_dir = repo / KB_DIR_REL
    kb_dir.mkdir(parents=True, exist_ok=True)
    # A .git entry marks the repo root for the scripts, so this needs no git.
    (repo / ".git").mkdir(exist_ok=True)

    lines = []
    for index, (rel, body) in enumerate(entries, start=1):
        # Each item carries a SECOND backticked span in its prose: only the
        # first token may ever be treated as an entry.
        lines.append(f"{index}. `{rel}` - synthetic description (see `docs/OTHER.md`)")
        if body is not None:
            target = (repo / rel) if rel in root_only else (kb_dir / rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")

    handoff = kb_dir / "HANDOFF.md"
    handoff.write_text(
        "# HANDOFF - synthetic\n"
        "**Verified at fake@0000000 on <date> by nobody**\n"
        "\n"
        "## 1. State\n"
        "- **Branch/HEAD:** `dev` @ `<SHA>`\n"
        "\n"
        f"{manifest_heading}\n" + "\n".join(lines) + "\n\n"
        "## 3. Open Questions (Verify before designing)\n"
        "- `NOT_A_MANIFEST_ENTRY.md` - prose, not a numbered item.\n",
        encoding="utf-8",
    )
    return repo, kb_dir, handoff


def _touch(path, offset_hours):
    """Set both atime and mtime to EPOCH + offset_hours."""
    stamp = EPOCH + int(offset_hours * 3600)
    os.utime(path, (stamp, stamp))


def _run(script, handoff, bundle=None):
    env = dict(os.environ)
    env.pop("KB_REPO_ROOT", None)
    env["HANDOFF_FILE"] = str(handoff)
    if bundle is not None:
        env["ONBOARDING_BUNDLE"] = str(bundle)
    return subprocess.run(
        [BASH, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_bundle_lists_files_in_manifest_order(tmp_path):
    _repo, _kb_dir, handoff = _make_project(tmp_path)
    bundle = tmp_path / "bundle.md"

    result = _run(BUILD_SCRIPT, handoff, bundle)

    assert result.returncode == 0, result.stderr
    text = bundle.read_text(encoding="utf-8")

    markers = [line for line in text.splitlines() if line.startswith("### File: ")]
    assert markers == [f"### File: {KB_DIR_REL}/{rel} ###" for rel, _ in MANIFEST]

    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions), "files are not in manifest order"

    body_positions = [text.index(body.strip()) for _, body in MANIFEST]
    assert body_positions == sorted(body_positions), "file bodies are out of order"

    # Prose backticks are not manifest entries.
    assert all("docs/OTHER.md" not in marker for marker in markers)

    # The newline-less body gains a terminating newline inside the bundle.
    assert "INFRA BODY\n" in text


def test_reported_byte_count_matches_bundle(tmp_path):
    _repo, _kb_dir, handoff = _make_project(tmp_path)
    bundle = tmp_path / "bundle.md"

    result = _run(BUILD_SCRIPT, handoff, bundle)

    assert result.returncode == 0, result.stderr
    reported_lines = [
        line for line in result.stdout.splitlines() if line.startswith("Bundle built: ")
    ]
    assert len(reported_lines) == 1
    reported = int(reported_lines[0].split()[2])
    assert reported == len(bundle.read_bytes())
    assert result.stdout.rstrip().endswith(f"Bundle built: {reported} bytes.")


def test_missing_referenced_file_fails_without_partial_bundle(tmp_path):
    _repo, _kb_dir, handoff = _make_project(
        tmp_path, entries=MANIFEST + [("missing_entry.md", None)]
    )
    bundle = tmp_path / "bundle.md"

    result = _run(BUILD_SCRIPT, handoff, bundle)

    assert result.returncode != 0
    assert "missing_entry.md" in result.stderr
    assert "HANDOFF.md" in result.stderr
    assert not bundle.exists()


def test_missing_section_heading_is_a_hard_error(tmp_path):
    _repo, _kb_dir, handoff = _make_project(
        tmp_path, manifest_heading="## Two. Read in this order"
    )
    bundle = tmp_path / "bundle.md"

    result = _run(BUILD_SCRIPT, handoff, bundle)
    assert result.returncode != 0
    assert "HANDOFF.md" in result.stderr
    assert "## 2." in result.stderr
    assert not bundle.exists()

    staleness = _run(STALENESS_SCRIPT, handoff)
    assert staleness.returncode != 0
    assert "HANDOFF.md" in staleness.stderr


def test_section_without_manifest_entries_is_a_hard_error(tmp_path):
    _repo, _kb_dir, handoff = _make_project(tmp_path, entries=[])
    bundle = tmp_path / "bundle.md"

    result = _run(BUILD_SCRIPT, handoff, bundle)

    assert result.returncode != 0
    assert "HANDOFF.md" in result.stderr
    assert not bundle.exists()


def test_entry_falls_back_to_repo_root(tmp_path):
    _repo, _kb_dir, handoff = _make_project(
        tmp_path,
        entries=MANIFEST + [("docs/ROOT_ONLY.md", "ROOT ONLY BODY\n")],
        root_only=("docs/ROOT_ONLY.md",),
    )
    bundle = tmp_path / "bundle.md"

    result = _run(BUILD_SCRIPT, handoff, bundle)

    assert result.returncode == 0, result.stderr
    text = bundle.read_text(encoding="utf-8")
    assert "### File: docs/ROOT_ONLY.md ###" in text
    assert "ROOT ONLY BODY" in text


def test_staleness_passes_when_all_files_are_older(tmp_path):
    _repo, kb_dir, handoff = _make_project(tmp_path)
    _touch(handoff, 0)
    _touch(kb_dir / MANIFEST[0][0], 0)  # equal mtime counts as FRESH
    for rel, _body in MANIFEST[1:]:
        _touch(kb_dir / rel, -5)

    result = _run(STALENESS_SCRIPT, handoff)

    assert result.returncode == 0
    assert result.stdout == "PASS: HANDOFF.md is up to date.\n"
    assert result.stderr == ""


def test_staleness_warns_when_a_file_is_newer(tmp_path):
    _repo, kb_dir, handoff = _make_project(tmp_path)
    _touch(handoff, 0)
    for rel, _body in MANIFEST:
        _touch(kb_dir / rel, -5)
    _touch(kb_dir / "vision_ledger.md", 3)

    result = _run(STALENESS_SCRIPT, handoff)

    assert result.returncode == 1
    assert result.stdout == (
        f"WARNING: {KB_DIR_REL}/vision_ledger.md is newer than HANDOFF.md. "
        "The handoff may be stale.\n"
    )


def test_staleness_warns_when_a_file_is_missing(tmp_path):
    _repo, kb_dir, handoff = _make_project(
        tmp_path, entries=MANIFEST[:3] + [("gone.md", None)]
    )
    _touch(handoff, 0)
    for rel, _body in MANIFEST[:3]:
        _touch(kb_dir / rel, -5)

    result = _run(STALENESS_SCRIPT, handoff)

    assert result.returncode == 1
    assert result.stdout == (
        f"WARNING: {KB_DIR_REL}/gone.md is missing. The handoff may be stale.\n"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_scripts_are_executable():
    for script in (BUILD_SCRIPT, STALENESS_SCRIPT):
        assert os.access(script, os.X_OK), f"{script} is not executable"
