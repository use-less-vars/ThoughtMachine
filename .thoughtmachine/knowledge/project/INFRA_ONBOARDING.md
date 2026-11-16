# INFRA_ONBOARDING — the reading index

*You are Infra. This file points at what to read. It does not summarize.
Read the files; this only tells you which, and why.*

## WORKFLOW — read before anything else.

1. Read §1 once at session start. Do not read §2 speculatively.
2. When a question arises, find the file in §2 that answers it. Read that file. Nothing else.
3. Before acting on a file you have not read this session, run `git log --oneline -3 <file>` — the map may be stale relative to what you are about to change.
4. Never cat a §3 file whole.
5. When you find a fact in the wrong file, fix it in the file, not by adding a note here.
6. **Always `git fetch` before declaring a knowledge-base or doc file missing.** Local `dev` was one
   merge behind (`27a9da52` vs `origin/dev` `cd06c87c`) and a file was reported missing that existed on
   `origin/dev`. This is class `handoff-claims-require-disk-read`
   (`knowledge/project/working_agreements.md:714`) applied to the checkout itself: the disk read must be
   of `origin/dev`, not of a stale local tree.

## §0 — Onboarding ceremony (your first turn)

The operator pastes you the previous engineer's handoff. Your first job: get the
lay of the land — not read everything, but read *enough*, and know where the rest is.

**Turn 0 — restart check.** Ask the operator: *"When was the machine last restarted?"* If any merge to `dev` has landed since the last restart and it touched runtime code (`web_ui/backend/`, `infra/`, `security/`, `tools/`, `agent/`, `docker_executor.py`), ask them to restart before proceeding. Silent staleness: the running process holds pre-merge modules; agent tests run against stale code; findings get misattributed.

**Turn 1 — ask for exactly one host block.** Do not ask file-by-file. Emit this;
the operator runs it and pastes the output back:

```bash
cd ~/PycharmProjects/ThoughtMachine-dev && \
echo "=== branch ===" && git rev-parse --abbrev-ref HEAD && \
echo "=== head ===" && git log --oneline -5 && \
echo "=== status ===" && git status --short && \
echo "=== remote dev ===" && git rev-parse origin/dev && \
echo "=== open PRs ===" && gh pr list --state open --limit 10 --json number,title,headRefName,mergeStateStatus && \
echo "=== CI on dev ===" && gh run list --branch dev --limit 5 && \
echo "=== KB mandatory ===" && \
for f in status.md current_map.md machine_model.md INFRA_ONBOARDING.md; do \
  p=".thoughtmachine/knowledge/project/$f"; \
  [ -f "$p" ] && echo "yes $(wc -c < "$p") $p" || echo "no  $p"; \
done && \
echo "=== rulings ===" && wc -c .thoughtmachine/working_docs/rulings-permission-facts.md && \
echo "=== KB tree ===" && find .thoughtmachine/knowledge -name '*.md' -type f | sort
```

**Turn 2 — ask for §1.** One `cat` block for the mandatory files. Read them.

**Turn 3 — send Main a hello.** Verified state, open questions, first task.

That is the whole ceremony: two host blocks, four turns. After it you are
onboarded. When a question arises later, find its file in §2 and read that file —
nothing else.

**Two path corrections carried forward.** The handoff that preceded this doc
named the wrong directories. The real paths:
- `rulings-permission-facts.md` lives in `working_docs/`, not `knowledge/project/`.
- The four large files live in `knowledge/personal/`, not `knowledge/project/`.

---

## §1 — Mandatory (read once)

**Enumerate with `find`, not `git ls-files`.** The KB is gitignored —
`git ls-files` will lie to you. Two files under `knowledge/project/` are
tracked by accident (`architecture_audit_2026-09.md` and
`development_guides.md`); they stay dirty in every `git status` — never
stage them.

**Disk check, last pass.** Every path in §1-§3 was checked against the working tree with `ls` and
`wc -c`; entries that were not there are marked **(absent — not on disk)**. They are marked, not
deleted, so the index still records what it expects to exist. Two caveats, both from rule 6 above:
that environment had no `git` binary, so no `git fetch` was possible and an absence means absent from
*that checkout*, not from `origin/dev`; and an absent entry is a **gap in the tree, not evidence that
the fact was retracted** — confirm against `origin/dev` before removing an entry or reasoning from it.

**First read `docs/ARCHITECTURE_MACHINE_MODEL.md`** — what runs where, the three
gates, the three container types. It is the only file that answers the plain
question, and it is short. Read it before the six below.

**Then read `docs/MACHINE_INVARIANTS_AND_LORE.md`** (31.1 KB) — the invariants that must not be
broken, the traps that have already caught engineers, and the `path:line` anchor for each claim. It
is the companion to the architecture doc: read it once here, then grep it by file name whenever a
claim needs its code site.

Six files. Read them once, in order, before your first task. No byte counts are quoted here — run
`wc -c <path>` for the current size. A size copied into this index is a fact whose authority is the
disk, and it goes stale silently.

**Five of the six were absent from the working tree at the last disk check** (see the note above).
The surviving mandatory read is `status.md`; where the rest are missing the substitute is
`docs/ARCHITECTURE_MACHINE_MODEL.md` plus `docs/MACHINE_INVARIANTS_AND_LORE.md`. Record an absent
file as absent — do not reconstruct its contents from memory or from a handoff that cites it.

- **`status.md`** (`knowledge/project/`) — current state, one page.
  **Cadence: updated at strand close, by whoever closes the strand.** If the
  last completed strand isn't reflected in it, that's a bug in the doc.
- **`machine_model.md`** (`knowledge/project/`) **(absent — not on disk)** — the six-layer promise
  table. What the machine promises, and how each promise is verified.
- **`design_layers_and_truth_ownership.md`** (`knowledge/project/`) **(absent — not on disk)** —
  Pass 2a. The layer ownership map and cross-layer Invariants 1–6.
- **`design_permission_facts_ownership.md`** (`knowledge/project/`) **(absent — not on disk)** —
  Pass 2c. Ownership of the four permission facts, artifacts A/B/C/D, the D4
  migration sequence, the D6 test-pin register.
- **`design_permission_bins_shape.md`** (`knowledge/project/`) **(absent — not on disk)** —
  Pass 2b. Shape A bins (vocabulary × bin), the `deny<ask<allow` meet, and
  T1/T2/T3 dissolve-only-in-steady-state. The destination.
- **`rulings-permission-facts.md`** (`working_docs/`) **(absent — not on disk)** — session rulings,
  append-only. R-1 (network default) is binding at D4 step 4.

**Plus one slot, filled by the operator each session:**

> *The current session handoff, pasted by the operator. Handoffs are
> session-local and are not persisted as files. When you write yours, make it
> a pointer to this document, not a re-derivation of it.*

---

## §2 — Read on question

One line per file: path · size · the question it answers · when to read it.
Read the matching section, not the whole file.

Sizes are quoted from the last disk check (`wc -c`, one decimal) and are the disk's fact, not this
file's — re-measure before leaning on one. Entries not on that tree are marked
**(absent — not on disk)** and carry no size.

- **`current_map.md`** (`knowledge/project/`) **(absent — not on disk)** · *Where am I?* · Read
  when disoriented. One page. **Cadence: phase-boundary.** If a thread isn't
  here, it isn't live.
- **`operator_notes.md`** (`knowledge/project/`) **(absent — not on disk)** · *Has the operator already said this?* · Grep before asking the operator a question they may have answered. **When picking up a queue item, grep this file for the topic first.**
- **`operator_observations.md`** (`knowledge/project/`, 152.3 KB) · *Is this already filed?* · The live ticket queue that `status.md` files its findings into. Read before filing a new bug; newest-at-bottom.
- **`container_controls_findings.md`** (`knowledge/project/`) **(absent — not on disk)** · *Container controls, collected (pre-brief).* · Read the matching section, not the whole. Pre-dates `architecture_audit_2026-09.md`.
- **`core.md`** (`knowledge/project/`) **(absent — not on disk)** · *Core-feature backlog.* · A workstream queue, not a state doc. Read the section for the strand.
- **`MANDATORY_STOP.md`** (`knowledge/project/`, 4.0 KB) · *What must I never
  do?* · Read before any action that could trip a stop condition.
- **`open_items_register.md`** (`knowledge/project/`) **(absent — not on disk)** · *Is this
  already tracked?* · Read before filing a new item. Sections 1–8; read the
  matching section.
- **`structural_debt_register.md`** (`knowledge/project/`) **(absent — not on disk)** · *Is this
  debt known?* · Read before proposing a refactor.
- **`working_agreements.md`** (`knowledge/project/`, 84.0 KB) · *What are the
  standing rules?* · **Caveat: dated history mixed with live rules — grep
  `^## ` for the index, read the live rules, skip the dated entries.**
- **`design_container_lifecycle_and_reclamation.md`** (`knowledge/project/`,
  26.7 KB) · *How is a container created, kept, and reclaimed?* · Read when
  working any container strand.
- **`design_intent_state_divergence_family.md`** (`knowledge/project/`) **(absent — not on disk)** · *Pass-1 edge rulings.* · Read when a fact at two sites disagrees.
- **`system_architecture.md`** (`knowledge/project/`) **(absent — not on disk)** · *How is the
  machine built?* · Read when the layer map (2a) isn't enough. Mixed: read the
  "Reference (evergreen)" section; dated sections are historical.
- **`security_architecture.md`** (`knowledge/project/`, 22.4 KB) · *How does
  the security layer work?* · Read when working the permission layer.
- **`architecture_audit_2026-09.md`** (`knowledge/project/`, 139.4 KB) · *The
  earlier structural audit.* · Read the TOC, then the matching section. The
  R-row table and cross-reference table are the load-bearing parts.
- **`docs/CONTRACTS.md`** (`docs/`, 13.9 KB) · *Which invariants does this machine
  actually guarantee, and what proves it?* · The contract ledger: each invariant
  with its enforcement site and a test that would fail if the enforcement were
  removed, plus (last section) what CI actually runs. Read before asserting that
  an invariant "holds". **Pointer check:** this is the ledger that exists; the
  `claim_ledger_design.md` cited by `v3_roadmap.md` and
  `architecture_audit_2026-09.md` is **absent — not on disk** anywhere in the tree.
- **`reliability_log.md`** (`knowledge/project/`) **(absent — not on disk)** · *Is this flake
  known?* · Read when investigating a recurring test failure.
- **`roadmap.md`** (`knowledge/project/`, 28.6 KB) · *Where is this going?* ·
  Read when orienting a strand against the arc. Note: "Current Status" is
  dated 2026-08-16.
- **`v3_roadmap.md`** (`knowledge/project/`, 17.1 KB) · *What are the V3
  phases?* · Read when a strand needs its phase context.
- **`v3_vision_full.md`** (`knowledge/project/`) **(absent — not on disk)** · *What is V3?* ·
  Read when a strand needs the destination.
- **`gen30_journey.md`** (`knowledge/project/`) **(absent — not on disk)** · *How did this arc
  come to be?* · Read for provenance when a decision's origin is unclear.
- **`development_guides.md`** (`knowledge/project/`, 52.0 KB) · *How do I
  build, test, and run?* · Read when setting up or running the suite. Read
  the specific topic heading; older sections are archived.
- **`run_design_pass.md`** (`knowledge/project/`) **(absent — not on disk)** · *How does a
  design pass work?* · Read before drafting one.

---

## §3 — Pointers (do not cat)

Measured at the last disk check (`wc -c` over the whole tree): `knowledge/` holds 33 files,
1,542,562 B in total, and **six** are ≥100 KB — four under `project/` (`archive_arch_a.md` 242.9 KB,
`operator_observations.md` 152.3 KB, `architecture_audit_2026-09.md` 139.4 KB, `archive_arch_b.md`
118.3 KB), one under `_archive/personal/` (`bugs_and_fixes.md` 131.3 KB), and one under `personal/`
(`lessons_learned.md` 105.5 KB). `personal/` itself holds exactly two files: `handoffs.md` (74.7 KB)
and `lessons_learned.md`. This section previously claimed thirteen files ≥100 KB split 7/5 between
`personal/` and `archive/`, and sized the four `personal/` files at 150-460 KB; neither matched the
disk. Grep `^## ` for headers; read the matching section; do not `cat` any of the six.

- **`lessons_learned.md`** (`personal/`, 105.5 KB) · Patterns and post-mortems. Read the
  matching section when a pattern repeats.
- **`bugs_and_fixes.md`** (`_archive/personal/`, 131.3 KB) · Filed bugs and their
  fixes. Read before filing a bug that may be a recurrence. **Note the `_archive/`
  prefix — it is not under `personal/`.**
- **`handoffs.md`** (`personal/`, 74.7 KB) · Previous engineers' handoffs. Read
  the newest entry for state, older entries for provenance.
- **`task_tracker.md`, `parking_lot.md`** · **Absent — not on disk** at the last
  check. A handoff that cites them cites something this tree does not contain; say
  so rather than reconstructing their contents.

---

# THE FOUR THINGS (Read before touching code)

**1. Container Types.**
* **Ephemeral:** Fresh container per `exec` call. Destroyed after. `/tmp` does not persist.
* **Persistent:** Workspace-level. Survives across exec calls.
* **Resource:** Sidecars (e.g., Postgres, Redis). Long-lived, network-addressable.

**2. Security vs. Trust Boundary.**
* **Security Boundary:** "Can the agent reach the host?" (Bypassing Docker). This is the catastrophic failure mode (rogue agent, poisoned prompt). It is guarded by the `host_bash` kill switch and the Host Port. If `host_bash` is off, the blast radius is contained to Docker.
* **Trust Boundary:** "Is the agent doing what the user allowed?" (e.g., writing files when the user set "read-only", or escaping the workspace directory). Failing closed here is a *success* of the permission system, not a security breach. 

**3. The Threat Model.**
Assume the agent is compromised (poisoned website, malicious prompt). It will try to exfiltrate secrets (vault, `.env`) or execute arbitrary code on the host. The engine's job is to make that impossible without an explicit operator override (the danger badge).

**4. Red Teaming is Infrastructure.**
Do not wait for a bug report. Periodically attack the boundary (DNS rebinding, SSRF, symlink escapes). The `test_isolation_boundary_e2e.py` suite is the automated version of this.
