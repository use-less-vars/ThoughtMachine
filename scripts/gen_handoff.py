#!/usr/bin/env python3
"""gen_handoff.py -- regenerate .thoughtmachine/knowledge/project/HANDOFF.md
from live git state. Per Gen 62 ruling: HANDOFF is generated, not hand-written.

The "## 2. Read in this order" section is the manifest that
scripts/build_onboarding.sh parses (see scripts/lib/kb_manifest.sh, which
requires a "## 2." heading and backticked numbered items) and that
scripts/check_kb_staleness.sh verifies for staleness.

Most manifest entries carry the digest of the file they name, recorded inline
as ``- sha256:<64 hex>``. There is deliberately no second lockfile: a lockfile
drifts away from the section it claims to guard, and the section is what the
bundle actually ships. The recorded digest is over the file's BYTES. The shell
side (``kb_sha256`` in scripts/lib/kb_manifest.sh -- it prefers ``shasum -a 256``
and falls back to ``sha256sum``, then ``openssl dgst -sha256``) must agree with ``hashlib.sha256`` here or
every check reports a false positive, so the rule is stated once per language
and nowhere else.

THE ADVISORY EXCEPTION: an entry listed in ``ADVISORY`` below is still listed
in the manifest but records NO digest. A recorded digest is a merge-time
tripwire -- "these are the bytes HANDOFF was generated from" -- which only
means something for a file whose bytes are STABLE across branches. The live
work queue (``PRIORITIES.md``) is not such a file: it is edited by every branch
that opens, closes or re-words a C-item and rewritten whenever dev is merged,
so its digest is branch-local BY DEFINITION. Hard-recording it would red the
contract test on any branch that is merely up to date with dev, and the only
cure anyone would find for that false alarm is hand-editing the router -- the
exact drift the digest exists to catch. So it stays first in the list (it is
the first thing to read) and simply carries no token. The twin rule on the
shell side is ``KB_ADVISORY_ENTRIES`` in scripts/lib/kb_manifest.sh.

The git facts (branch head, subject, recent commits) are read from git but each
one is overridable from the command line, so the generator also runs in a
checkout with no git binary (see C34).

Asserts every C-item it names exists in PRIORITIES.md. Exit non-zero if any
required file is missing or the manifest would be empty.
"""
import argparse
import hashlib
import pathlib
import re
import subprocess
import sys

KB = ".thoughtmachine/knowledge/project"

# (repo-relative path, one-line prose) -- the reading order, in order.
MANIFEST = [
    (f"{KB}/PRIORITIES.md", "the live work queue. **Read first.**"),
    ("docs/CONTROL_PLANE.md", "the pause/stop/timeout contract. Doctrine."),
    (f"{KB}/MANDATORY_STOP.md", "the read-on-entry rules; none of them are optional."),
    (f"{KB}/architecture_invariants.md", "the invariants a design must not re-derive."),
    ("docs/ARCHITECTURE_MACHINE_MODEL.md", "what runs where, and what stops it."),
    (f"{KB}/machine_history_and_vision.md", "the incidents behind the rules, and what they are for."),
    (f"{KB}/vision_ledger.md", "the five standing commitments (V1-V5)."),
    (f"{KB}/ORCHESTRATOR_GUIDE.md", "how to run workers without believing the brief."),
    (f"{KB}/CI_PROCESS.md", "what CI actually runs, and what this repo cannot see."),
    (f"{KB}/INFRA_ONBOARDING.md", "the environment you are working in."),
]

# Manifest entries that are LISTED but carry NO recorded digest.
#
# A recorded digest is a merge-time tripwire: it means "these are the bytes
# HANDOFF was generated from". That tripwire is only worth anything for a file
# whose bytes are STABLE across branches -- there, any drift means the router
# describes a tree it was not generated from. PRIORITIES.md is the one member
# that is not stable: it is the live work queue, edited by every branch that
# opens, closes or re-words a C-item and rewritten whenever dev is merged. Its
# digest is therefore branch-local BY DEFINITION, and hard-recording it
# guarantees a red contract test on any branch that is merely up to date with
# dev -- teaching everyone to hand-edit the router, which is the drift the
# digest exists to stop. So it stays listed (it is the first thing to read)
# and just carries no digest.
#
# Cross-language: the shell side must agree -- see ``KB_ADVISORY_ENTRIES`` in
# scripts/lib/kb_manifest.sh. The rule is stated once per language and nowhere
# else, exactly like the sha256 rule above.
ADVISORY = frozenset({f"{KB}/PRIORITIES.md"})


def sh(*args: str) -> str:
    # encoding pinned: git emits UTF-8, but a Windows/macOS locale default
    # (cp1252, ascii) would decode it with the wrong codec and either raise or
    # mangle non-ASCII subjects.
    return subprocess.check_output(args, text=True, encoding="utf-8").strip()


def _parse_open_items(active: str) -> list:
    """Return ``[(cid, title), ...]`` for every open C-item in ``active``.

    The unit of parsing is a BLOCK, not a physical line: a block starts at any
    line beginning ``- `` and absorbs every following line up to the next such
    line. A block whose bold title wraps across two lines is therefore parsed
    intact instead of vanishing. The done-marker test runs over the whole
    joined block, and the title is the same capture as before.
    """
    blocks = []
    current = None
    for line in active.splitlines():
        if line.startswith("- "):
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    open_items = []
    for block_lines in blocks:
        block = " ".join(" ".join(block_lines).split())
        m = re.match(r"^- \*\*(C\d+):\s+(.*?)\*\*", block)
        if not m:
            continue
        cid, body = m.group(1), m.group(2)
        if re.search(r"\*\*Landed\*\*", block):
            continue
        open_items.append((cid, " ".join(body.split()).strip()))
    return open_items


def sha256_file(path: pathlib.Path) -> str:
    """Hex sha256 of the file's bytes. Same rule as ``kb_sha256`` in shell."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(root, open_items, dev_sha: str, dev_subj: str, recent: str) -> str:
    """Return the whole HANDOFF text (trailing newline included)."""
    out = [
        "# HANDOFF -- generated, do not hand-edit",
        "",
        "**Generated by `scripts/gen_handoff.py`. Run the generator to refresh.**",
        "",
        f"- `dev` at `{dev_sha}` -- *{dev_subj}*",
        "",
        "## Last five commits on dev",
        "",
        "```",
        recent,
        "```",
        "",
        "## 2. Read in this order",
        "",
    ]
    for i, (rel, prose) in enumerate(MANIFEST, 1):
        line = f"{i}. `{rel}` -- {prose}"
        # Advisory entries are listed but carry no digest: their digest would be
        # branch-local, so recording it would make every branch that is merely
        # up to date with dev fail the contract test (see ADVISORY above).
        if rel not in ADVISORY:
            line += f" - sha256:{sha256_file(root / rel)}"
        out.append(line)
    out += ["", "## First task (from the live queue)", ""]
    for cid, body in open_items:
        out.append(f"- **{cid}** -- {body}")
    out += [
        "",
        "## Rules of Engagement (Gen 62 Protocol)",
        "",
        "- **The Operator is a relay, not a bash environment.** Do not ask the Operator to run `sed`, `python`, `awk` or multi-line shell to accomplish a change; dispatch the Agent instead.",
        "- **Heredocs are banned.** They mangle the Operator's terminal. Any change over five lines is written and committed by the Agent, not pasted by the Operator.",
        "- **Investigation is delegated.** The Engineer reads diffs and summaries, not source files; source reading belongs to the Agent.",
        "- **Onboarding reading is 100-150 kB in one to three massive chunks.** Do not chop the reading into thirty small turns.",
    ]
    out += [
        "",
        "## Carried traps (do not re-discover)",
        "",
        "- `PRIORITIES.md` did not contain C25 or \"Phase 10\" at the Gen 62 correction. The pre-Gen-62 handoff hallucinated both.",
        "- `docs/CONTROL_PLANE.md` section 10.6 blocks Option A (None sentinel) via `tests/test_state_pause_restriction.py:104-108`. Proceed with Option B.",
        "- `PAUSE_PROPAGATION_DESIGN.md` Stop wording contradicts `CONTROL_PLANE.md` section 4.3. Section 8 item 4 is **held** pending operator review.",
        "- `docs/VISION_LEDGER.md` does not exist. The Vision Ledger is `.thoughtmachine/knowledge/project/vision_ledger.md` and holds V-items, not C-items.",
        "",
        "## Do not re-litigate",
        "",
        "- **Pause = soft wind-down. Stop = hard abort. Timeout = detach.**",
        "- **The 4 Lego Pieces are law:** Persistent State, Interrupt Signal, Tool Return Contract, Loop Gate.",
        "- **The container envelope is its birth certificate.** Stop -> Edit -> Restart (recreate).",
        "",
        "## Message to the next generation",
        "",
        "- **The runtime-mode seam exists; the process does not.** `DEFAULT_RUNTIME_MODE` is",
        "  `MODE_PROCESS` and `resolve_runtime_mode()` is fail-closed, so worker handles are",
        "  *labelled* process - but the production spawn site builds the handle and never",
        "  starts it (`tools/workspace/worker_thread.py:6827-6843`). Workers still run inside",
        "  the supervisor's interpreter. Real process creation is",
        "  `WorkerRuntimeHandle.spawn` (`tools/workspace/worker_runtime.py:595`), whose only",
        "  non-test caller is `scripts/prove_worker_pid.py`. See the MODE_PROCESS follow-ups",
        "  item in PRIORITIES.md.",
        "- **Two different objects are called `CapabilityRegistry`.** The live one is",
        "  `tools/capability_registry.py`: resolved before worker construction, and a",
        "  `CapabilityResolveError` aborts the spawn. The `CapabilityRegistry` deprecated in",
        "  `thoughtmachine/security.py` is a different object with the same name. Always say",
        "  which one you mean.",
        "- **C60 is fixed; CI stability is a prediction, not a fact.** The",
        "  `ProviderNotFoundError: Provider 'openai_compatible' not found` signature is gone",
        "  (fixtures no longer bare-assign `ProviderFactory._providers`). The suite is still",
        "  not order-independent: a single-process run OOM-kills at ~79%, and the chunked run",
        "  records 10 failures whose polluting file is not yet identified. Do not spend the",
        "  next generation's budget trusting 'CI is green' until someone records a green run.",
        "- **C61 is the largest open correctness item, and it is two defects.** *Defect 1",
        "  (worker side):* the budget is enforced at ADMISSION time only, so a tool call",
        "  already dispatched outlives the deadline. Measured: a 200 s sleep under a 60 s effective",
        "  budget completed in **205.63 s** (`timed_out: false`, exit 0). That number is the tool call's",
        "  own wall clock (the 200 s command plus container overhead), NOT caller block time. *Defect 2",
        "  (caller side):* the caller waits `effective + QUERY_WAIT_GRACE_SECONDS` (a 60 s grace,",
        "  `agent/config/defaults.py:37`, applied once in `tools/workspace/worker_timeout.py:95-97`), so",
        "  a 60 s budget blocks the caller ~120 s. That grace is deliberate: it lets a soft-timed-out",
        "  worker finish and return its envelope instead of the caller abandoning it. The two numbers are",
        "  separate measurements of separate defects; never read them as one compounding failure.",
        "- **Read the manuals before touching their subjects.** `machine_history_and_vision.md`",
        "  section 3.2 is required reading before `thoughtmachine/host_port.py`.",
        "  `ORCHESTRATOR_GUIDE.md` is the operating manual for this role. `HANDOFF.md` is only",
        "  the router; long prose belongs in the guide.",
        "",
        "---",
        "",
        f"*Generated at `{dev_sha}`. Run `python scripts/gen_handoff.py` to refresh; do not hand-edit.*",
        "",
    ]
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the onboarding router (HANDOFF.md).",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="repo root (default: git rev-parse --show-toplevel)",
    )
    parser.add_argument(
        "--dev-sha",
        default=None,
        help="branch head to record (default: git rev-parse --short dev)",
    )
    parser.add_argument(
        "--dev-subject",
        default=None,
        help="head commit subject (default: git log -1 --format=%%s dev)",
    )
    parser.add_argument(
        "--recent-file",
        default=None,
        help="file holding the 'last five commits' block (default: git log -5 --oneline dev)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print the generated text instead of writing HANDOFF.md",
    )
    args = parser.parse_args(argv)

    if args.root is not None:
        root = pathlib.Path(args.root).resolve()
    else:
        root = pathlib.Path(sh("git", "rev-parse", "--show-toplevel"))
    priorities = root / KB / "PRIORITIES.md"
    handoff = root / KB / "HANDOFF.md"
    control_plane = root / "docs" / "CONTROL_PLANE.md"
    required = [priorities, control_plane] + [root / rel for rel, _ in MANIFEST]
    for path in required:
        if not path.is_file():
            print(f"ERROR: {path} missing; refusing to generate.", file=sys.stderr)
            return 1

    if args.dev_sha is not None:
        dev_sha = args.dev_sha
    else:
        dev_sha = sh("git", "rev-parse", "--short", "dev")
    if args.dev_subject is not None:
        dev_subj = args.dev_subject
    else:
        dev_subj = sh("git", "log", "-1", "--format=%s", "dev")
    if args.recent_file is not None:
        recent = pathlib.Path(args.recent_file).read_text(encoding="utf-8").strip()
    else:
        recent = sh("git", "log", "-5", "--oneline", "dev")

    active = priorities.read_text(encoding="utf-8").split("## Archived")[0]
    open_items = _parse_open_items(active)
    if not open_items:
        print("ERROR: no open C-items parsed; refusing.", file=sys.stderr)
        return 1

    text = render(root, open_items, dev_sha, dev_subj, recent)
    if args.stdout:
        # Emit UTF-8 BYTES, not locale-encoded text: the router is compared to
        # the checked-in file byte for byte, and a locale stdout (cp1252 on
        # Windows) would raise UnicodeEncodeError or mangle the members'
        # non-ASCII prose before it ever reached the pipe.
        sys.stdout.flush()
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    handoff.write_text(text, encoding="utf-8", newline="\n")
    print(
        f"Wrote {handoff} ({len(text.splitlines())} lines, "
        f"{len(open_items)} open C-items)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
