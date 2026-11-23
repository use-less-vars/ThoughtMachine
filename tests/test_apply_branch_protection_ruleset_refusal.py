"""Mutation guard for the refusal gate in scripts/apply_branch_protection.sh.

That script speaks the **classic** branch-protection endpoint, which on this
repo answers 404. What actually decides a merge is a repository ruleset, and a
ruleset overrides the classic layer. So a script that PUTs a classic payload,
gets a 2xx and prints "applied" is a silent no-op dressed as enforcement --
and worse, it is the exact artefact a reader would cite as "the gates are
enforced". The behaviour pinned here is the refusal that replaced it, plus the
list it refuses to overstate:

  1. a governing ruleset -> exit 3, the ruleset id named, no payload printed;
  2. no ``gh`` on PATH -> still exit 3, and the message says the RULESET layer
     is the one it could not observe (not "no ruleset governs this branch");
  3. a ``gh`` that failed -> exit 3 as well; a failed read must never be read
     as "ungoverned";
  4. an observed *absence* of branch rulesets -> the dry run proceeds, exit 0;
  5. ``--force-classic`` -> proceeds, and says out loud that the layer was not
     confirmed absent;
  6. the six produced-but-not-required contexts are NEVER in the payload's
     ``contexts`` array, and are still declared -- in ``CANDIDATE_CHECKS``, i.e.
     moved, not deleted. Re-adding one is a ruleset edit, not a script edit;
  7. ``--verify-names`` labels those six as candidates and goes red (exit 1)
     when a required context is missing from the dump it is given. That case is
     exercised by omitting ``Mutation gate (contracts)`` from a synthetic dump;
     the context itself IS produced on this tree (see
     ``test_the_mutation_gate_context_has_a_producing_job``), so the exit 1 is
     about the dump, not about the tree.

Every sweep below asserts the size of the set it walks first, so a script that
stopped printing a list cannot make these cases pass vacuously.

Hermetic and fast by construction: the real script runs under the real ``bash``
with a scrubbed environment, a PATH built from symlinks to a handful of
coreutils, and no ``gh`` unless the case plants one (a symlink to ``true`` or
``false`` -- never a script written to disk, because a noexec ``/tmp`` would
turn that into a different test). No network, no GitHub, no vault, no Docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "apply_branch_protection.sh"
BASH = shutil.which("bash")

#: The contexts ruleset 24214086 requires: 13 stored entries, 13 unique
#: (measured 2026-11-23T15:20+01:00). Hard-coded on purpose -- this is the
#: measured set, not a restatement of whatever the script happens to say.
REQUIRED = (
    "Contract gate (docs/CONTRACTS.md)",
    "Install + check-only",
    "Installer version-contract assertions",
    "Linux - install/startup smoke (blocking)",
    "Linux install + check-only + backend smoke",
    "Mutation gate (contracts)",
    "Ubuntu - full test suite",
    "Unit tests - Python 3.11",
    "Unit tests - Python 3.12",
    "Unit tests - Python 3.13",
    "Unit tests - Python 3.14",
    "macOS launcher preflight (gating)",
    "web-ui-frontend-suite",
)

#: Produced by real jobs, measured ABSENT from the ruleset. Declared, not
#: required: they run, they can go red, they stop no merge.
CANDIDATES = (
    "Linux - auth e2e (browser)",
    "Windows - import smoke",
    "Windows - thin pytest slice",
    "Windows Python 3.14 gate proof",
    "Windows venv + smoke script",
    "web-ui-backend-suite",
)

#: Left out of both lists on purpose; unclassified as required, by decision.
MACOS_ADVISORY = (
    "macOS - full test suite (advisory)",
    "macOS - install/startup smoke (advisory)",
    "macOS - Docker availability probe (advisory)",
)

_TOOLS = ("sed", "grep", "tr", "awk")


def _have(tool: str) -> str:
    real = shutil.which(tool)
    assert real, f"required to run the script under test: {tool!r} is not on PATH"
    return real


def _sandbox(tmp_path: Path, gh_target: str | None = None) -> Path:
    """A PATH directory with the few coreutils the script needs, and opt-in gh.

    Symlinks, not copies: with ``/tmp`` mounted noexec a byte-identical file on
    disk would fail to exec for reasons that have nothing to do with the script.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in _TOOLS:
        link = bin_dir / tool
        if not link.is_symlink():
            os.symlink(_have(tool), link)
    gh_link = bin_dir / "gh"
    gh_link.unlink(missing_ok=True)
    if gh_target is not None:
        os.symlink(gh_target, gh_link)
    return bin_dir


def run_script(
    tmp_path: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    gh_target: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the real script with a scrubbed env: no gh, no RULESET_IDS, no network."""
    bin_dir = _sandbox(tmp_path, gh_target)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    env.update(env_extra or {})
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        cwd=str(tmp_path),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def _payload_contexts(stdout: str) -> list[str]:
    """The ``contexts`` array the dry run printed, as strings."""
    assert '"contexts": [' in stdout, "the dry run printed no contexts array"
    body = stdout.split('"contexts": [', 1)[1].split("]", 1)[0]
    names = [line.strip().rstrip(",").strip() for line in body.splitlines()]
    return [n.strip('"') for n in names if n]


def _array_block(source: str, name: str) -> str:
    """The literal ``NAME=( ... )`` block of the script, comments included."""
    marker = f"\n{name}=("
    assert marker in source, f"{name} is gone from {SCRIPT.name}"
    return source.split(marker, 1)[1].split("\n)", 1)[0]


# --- the refusal, in each direction it has to be able to fail -----------------


def test_a_governing_ruleset_is_refused_by_name(tmp_path):
    result = run_script(tmp_path, env_extra={"RULESET_IDS": "24214086"})
    assert result.returncode == 3, result.stderr
    assert "REFUSING" in result.stderr
    assert "24214086" in result.stderr
    assert "CLASSIC" in result.stderr
    # Nothing was built or printed: the payload path is not reached at all.
    assert '"contexts"' not in result.stdout
    assert "applied" not in result.stdout


def test_every_governing_ruleset_id_is_named(tmp_path):
    result = run_script(tmp_path, env_extra={"RULESET_IDS": " 24214086 , 24214087 "})
    assert result.returncode == 3, result.stderr
    assert "24214086" in result.stderr
    assert "24214087" in result.stderr


def test_no_gh_refuses_and_names_the_layer_it_could_not_observe(tmp_path):
    # No RULESET_IDS and no gh on PATH: the script cannot see the ruleset layer.
    result = run_script(tmp_path)
    assert result.returncode == 3, result.stderr
    assert "could not observe the RULESET layer" in result.stderr
    assert "no gh on PATH" in result.stderr
    assert "repos/use-less-vars/ThoughtMachine/rulesets" in result.stderr
    assert '"contexts"' not in result.stdout


def test_a_failed_gh_read_is_not_read_as_ungoverned(tmp_path):
    # gh on PATH but the read fails (a token without ruleset scope, an outage):
    # the gap must surface as a refusal, never as "no ruleset governs dev".
    result = run_script(tmp_path, gh_target=_have("false"))
    assert result.returncode == 3, result.stderr
    assert "could not observe the RULESET layer" in result.stderr
    assert "failed" in result.stderr
    assert '"contexts"' not in result.stdout


def test_an_observed_absence_of_branch_rulesets_lets_the_dry_run_run(tmp_path):
    # gh answers and lists nothing: the classic layer is the only layer, so the
    # script may speak -- and still sends nothing without --apply.
    result = run_script(tmp_path, gh_target=_have("true"))
    assert result.returncode == 0, result.stderr
    assert "CLASSIC branch protection" in result.stdout
    assert "nothing was sent" in result.stdout


def test_a_recorded_observation_of_none_lets_the_dry_run_run(tmp_path):
    # RULESET_IDS set but empty is the recorded "observed: none" value.
    result = run_script(tmp_path, env_extra={"RULESET_IDS": ""})
    assert result.returncode == 0, result.stderr
    assert '"contexts"' in result.stdout


def test_force_classic_proceeds_and_says_the_layer_was_not_confirmed_absent(tmp_path):
    result = run_script(tmp_path, "--force-classic")
    assert result.returncode == 0, result.stderr
    assert "WARNING --force-classic" in result.stderr
    assert "RULESET layer was not confirmed absent" in result.stderr
    assert "CLASSIC branch protection" in result.stdout


# --- the list: required means required ----------------------------------------


def test_the_payload_requires_the_thirteen_and_none_of_the_candidates(tmp_path):
    result = run_script(tmp_path, "--force-classic")
    assert result.returncode == 0, result.stderr
    contexts = _payload_contexts(result.stdout)
    # Size first: the sweeps below are meaningless against an empty list.
    assert len(REQUIRED) == 13
    assert len(CANDIDATES) == 6
    assert len(contexts) == 13, contexts
    assert contexts == list(REQUIRED)
    for name in CANDIDATES:
        assert name not in contexts, f"{name} is produced but NOT required by the ruleset"


def test_the_candidates_were_moved_out_of_the_required_list_not_deleted(tmp_path):
    source = SCRIPT.read_text(encoding="utf-8")
    required_block = _array_block(source, "REQUIRED_CHECKS")
    candidate_block = _array_block(source, "CANDIDATE_CHECKS")
    assert len(CANDIDATES) == 6
    for name in CANDIDATES:
        assert name not in required_block, f"{name} is back in REQUIRED_CHECKS"
        assert name in candidate_block, f"{name} vanished from the script entirely"


# --- --verify-names: both directions, and the bucket that is new --------------


def test_verify_names_labels_the_candidates_instead_of_requiring_them(tmp_path):
    dump = tmp_path / "observed.txt"
    dump.write_text("\n".join(REQUIRED + CANDIDATES + MACOS_ADVISORY) + "\n", encoding="utf-8")
    result = run_script(tmp_path, "--verify-names", str(dump))
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(CANDIDATES) == 6
    assert result.stdout.count("NOT REQUIRED") == 6
    assert result.stdout.count("NOT GATED") == len(MACOS_ADVISORY)


def test_verify_names_fails_when_a_required_context_is_missing_from_the_dump(
    tmp_path,
):
    # A required context absent from the dump parks every PR at "Expected", so
    # the honest reading of such a dump is RED. Exercised with
    # `Mutation gate (contracts)` omitted -- NOT because this tree lacks a
    # producer for it (it has one, pinned below), but because it is a required
    # name whose absence the script must not swallow.
    dump = tmp_path / "observed.txt"
    missing = [n for n in REQUIRED if n != "Mutation gate (contracts)"]
    assert len(REQUIRED) == 13
    dump.write_text("\n".join(missing) + "\n", encoding="utf-8")
    result = run_script(tmp_path, "--verify-names", str(dump))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ABSENT" in result.stdout
    assert "Mutation gate (contracts)" in result.stdout



# --- the corrected provenance: the required context HAS a producer here -------


def test_the_mutation_gate_context_has_a_producing_job():
    """`Mutation gate (contracts)` is required AND produced on this tree.

    An earlier revision of the script asserted the opposite -- that no job in
    this tree reports that context, so `--verify-names` should always call it
    ABSENT. That was measured on a tree that predated the job. This test reads
    the workflow on disk, so the claim cannot go stale silently in either
    direction: no job named that -> this fails, and so does a job whose run step
    stopped invoking the gate script.
    """
    workflow = REPO_ROOT / ".github" / "workflows" / "contract-gate.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "Mutation gate (contracts)" in REQUIRED
    assert "    name: Mutation gate (contracts)\n" in text, (
        "no job in contract-gate.yml carries the required check name; "
        "--verify-names would report it ABSENT on every PR"
    )
    assert "scripts/mutation_gate.py" in text, (
        "the job that reports the required context no longer runs the gate script"
    )

