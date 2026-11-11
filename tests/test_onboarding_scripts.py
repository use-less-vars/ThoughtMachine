"""Hermetic contract tests for the KB onboarding scripts.

Covers ``scripts/build_onboarding.sh`` and ``scripts/check_kb_staleness.sh``
against a synthetic project tree under ``tmp_path`` -- the real repository's
HANDOFF.md and the real member files are never consulted, so these tests are
deterministic on any checkout (the real-repo contract lives in
``tests/test_onboarding_bundle_contract.py``).

The contract exercised:

  * the bundle leads with the router itself, verbatim and first, then every
    file section 2 of the router lists, in manifest order;
  * the manifest is the numbered, first-token-backticked items of section 2
    (``## 2. <title>``; the title itself is deliberately not matched because
    humans rewrite it);
  * entries resolve next to the router first, then at the repo root;
  * an absent section, an empty manifest, a missing referenced file, and a
    manifest that lists the router itself are hard errors -- never a silently
    empty, partial or duplicated bundle;
  * staleness is CONTENT-addressed: each entry line records
    ``- sha256:<64 hex>`` and the gate compares that digest with the file's
    bytes. ``--check`` (the default) exits 0 only when every digest matches;
    a changed file, a missing file and an entry with NO recorded digest all
    exit 1 (fail closed -- an entry that lost its hash must never pass
    forever). ``--update`` rewrites just those tokens, atomically and only
    after every entry has hashed cleanly; an unknown argument is a usage
    error on stderr with a non-zero exit.
"""

import hashlib
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

KB_DIR_REL = ".thoughtmachine/knowledge/project"

# (path relative to the router's directory, body). The third body deliberately
# has no trailing newline so the bundle's newline repair is exercised.
MANIFEST = [
    ("PRIORITIES.md", "PRIORITIES BODY\nsecond line\n"),
    ("vision_ledger.md", "VISION BODY\n"),
    ("INFRA_ONBOARDING.md", "INFRA BODY"),
    ("status.md", "STATUS BODY\n"),
]


def _make_project(
    tmp_path,
    entries=None,
    manifest_heading="## 2. Read in this order",
    root_only=(),
    record_hashes=True,
):
    """Build a fake repo + router; returns (repo, kb_dir, handoff_path).

    ``record_hashes`` appends the ``- sha256:<hex>`` token the real generator
    emits, so the staleness gate has content to compare instead of exercising
    only its fail-closed branch.
    """
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
        line = f"{index}. `{rel}` - synthetic description (see `docs/OTHER.md`)"
        if body is not None:
            target = (repo / rel) if rel in root_only else (kb_dir / rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            if record_hashes:
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                line += f" - sha256:{digest}"
        lines.append(line)

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


def _run(script, handoff, bundle=None, args=()):
    env = dict(os.environ)
    env.pop("KB_REPO_ROOT", None)
    env["HANDOFF_FILE"] = str(handoff)
    if bundle is not None:
        env["ONBOARDING_BUNDLE"] = str(bundle)
    return subprocess.run(
        [BASH, str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_bundle_leads_with_the_router_then_manifest_order(tmp_path):
    _repo, _kb_dir, handoff = _make_project(tmp_path)
    bundle = tmp_path / "bundle.md"

    result = _run(BUILD_SCRIPT, handoff, bundle)

    assert result.returncode == 0, result.stderr
    text = bundle.read_text(encoding="utf-8")

    markers = [line for line in text.splitlines() if line.startswith("### File: ")]
    assert markers == [f"### File: {KB_DIR_REL}/HANDOFF.md ###"] + [
        f"### File: {KB_DIR_REL}/{rel} ###" for rel, _ in MANIFEST
    ]

    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions), "files are not in manifest order"

    # The router leads the members and is carried verbatim.
    assert text.index("## 1. State") < positions[1]
    assert "# HANDOFF - synthetic" in text
    assert "## 3. Open Questions (Verify before designing)" in text
    # "Verbatim" means the whole router as one contiguous block, byte for byte
    # -- not merely that a few of its lines can be found somewhere in the text.
    assert handoff.read_text(encoding="utf-8") in text

    body_positions = [text.index(body.strip()) for _, body in MANIFEST]
    assert body_positions == sorted(body_positions), "file bodies are out of order"

    # Prose backticks are not manifest entries.
    assert all("docs/OTHER.md" not in marker for marker in markers)

    # The newline-less body gains a terminating newline inside the bundle.
    assert "INFRA BODY\n" in text

    # The preamble counts the router plus every member.
    assert f"Files: {len(MANIFEST) + 1}\n" in text


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
    # A previously built bundle must survive a failed run untouched, and no
    # temporary file may be left behind: "no partial bundle" is about the
    # bundle that already existed, not only about the one that never appeared.
    previous = "# PREVIOUS GOOD BUNDLE\nkeep me\n"
    bundle.write_text(previous, encoding="utf-8")

    result = _run(BUILD_SCRIPT, handoff, bundle)

    assert result.returncode != 0
    assert "missing_entry.md" in result.stderr
    assert "HANDOFF.md" in result.stderr
    assert bundle.read_text(encoding="utf-8") == previous
    assert list(tmp_path.glob("bundle.md.tmp.*")) == []


def test_manifest_listing_the_router_itself_is_a_hard_error(tmp_path):
    """The router already leads the bundle; listing it again would duplicate it."""
    _repo, _kb_dir, handoff = _make_project(
        tmp_path, entries=MANIFEST + [("HANDOFF.md", None)]
    )
    bundle = tmp_path / "bundle.md"

    result = _run(BUILD_SCRIPT, handoff, bundle)

    assert result.returncode != 0
    assert "lists the router itself" in result.stderr
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


def test_check_passes_when_recorded_hashes_match(tmp_path):
    _repo, _kb_dir, handoff = _make_project(tmp_path)

    result = _run(STALENESS_SCRIPT, handoff, args=("--check",))

    assert result.returncode == 0
    assert result.stdout == "PASS: HANDOFF.md is up to date.\n"
    assert result.stderr == ""


def test_no_argument_is_an_alias_for_check(tmp_path):
    _repo, _kb_dir, handoff = _make_project(tmp_path)

    implicit = _run(STALENESS_SCRIPT, handoff)
    explicit = _run(STALENESS_SCRIPT, handoff, args=("--check",))

    assert implicit.returncode == explicit.returncode == 0
    assert implicit.stdout == explicit.stdout
    assert implicit.stderr == explicit.stderr == ""


def test_check_fails_when_a_file_changed(tmp_path):
    _repo, kb_dir, handoff = _make_project(tmp_path)
    (kb_dir / "vision_ledger.md").write_text("VISION BODY, EDITED\n", encoding="utf-8")

    result = _run(STALENESS_SCRIPT, handoff, args=("--check",))

    assert result.returncode == 1
    assert f"{KB_DIR_REL}/vision_ledger.md" in result.stdout
    assert "changed since HANDOFF.md recorded sha256:" in result.stdout
    assert "The handoff may be stale." in result.stdout


def test_check_fails_when_a_file_is_missing(tmp_path):
    _repo, _kb_dir, handoff = _make_project(
        tmp_path, entries=MANIFEST[:3] + [("gone.md", None)]
    )

    result = _run(STALENESS_SCRIPT, handoff, args=("--check",))

    assert result.returncode == 1
    assert result.stdout == (
        f"WARNING: {KB_DIR_REL}/gone.md is missing. The handoff may be stale.\n"
    )


def test_check_fails_closed_when_no_hash_is_recorded(tmp_path):
    """A lost digest must be STALE, never 'nothing to compare'."""
    _repo, _kb_dir, handoff = _make_project(tmp_path, record_hashes=False)

    result = _run(STALENESS_SCRIPT, handoff, args=("--check",))

    assert result.returncode == 1
    assert "no sha256 recorded, run --update" in result.stdout


def test_update_records_hashes_and_then_check_passes(tmp_path):
    _repo, kb_dir, handoff = _make_project(tmp_path, record_hashes=False)

    stale = _run(STALENESS_SCRIPT, handoff, args=("--check",))
    assert stale.returncode == 1

    update = _run(STALENESS_SCRIPT, handoff, args=("--update",))

    assert update.returncode == 0, update.stderr
    assert update.stdout == (
        f"Updated {len(MANIFEST)} recorded sha256 values in {handoff}.\n"
    )

    text = handoff.read_text(encoding="utf-8")
    # Only the digest tokens change; the rest of the router survives verbatim.
    assert "## 1. State" in text
    assert "- `NOT_A_MANIFEST_ENTRY.md` - prose, not a numbered item." in text
    for rel, _body in MANIFEST:
        digest = hashlib.sha256((kb_dir / rel).read_bytes()).hexdigest()
        assert f"`{rel}`" in text
        assert f"- sha256:{digest}\n" in text

    fresh = _run(STALENESS_SCRIPT, handoff, args=("--check",))
    assert fresh.returncode == 0
    assert fresh.stdout == "PASS: HANDOFF.md is up to date.\n"


def test_update_refuses_to_rewrite_a_partial_router(tmp_path):
    _repo, _kb_dir, handoff = _make_project(
        tmp_path, entries=MANIFEST[:3] + [("gone.md", None)], record_hashes=False
    )
    before = handoff.read_bytes()

    result = _run(STALENESS_SCRIPT, handoff, args=("--update",))

    assert result.returncode != 0
    assert "gone.md" in result.stderr
    assert handoff.read_bytes() == before


def test_unknown_argument_is_a_usage_error(tmp_path):
    _repo, _kb_dir, handoff = _make_project(tmp_path)

    result = _run(STALENESS_SCRIPT, handoff, args=("--bogus",))

    assert result.returncode == 2
    assert "unknown argument: --bogus" in result.stderr
    assert "usage:" in result.stderr
    assert result.stdout == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_scripts_are_executable():
    for script in (BUILD_SCRIPT, STALENESS_SCRIPT):
        assert os.access(script, os.X_OK), f"{script} is not executable"
