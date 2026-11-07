# INFRA_ONBOARDING — the reading index

*You are Infra. This file points at what to read. It does not summarize.
Read the files; this only tells you which, and why.*

## WORKFLOW — read before anything else.

1. Read §1 once at session start. Do not read §2 speculatively.
2. When a question arises, find the file in §2 that answers it. Read that file. Nothing else.
3. Before acting on a file you have not read this session, run `git log --oneline -3 <file>` — the map may be stale relative to what you are about to change.
4. Never cat a §3 file whole.
5. When you find a fact in the wrong file, fix it in the file, not by adding a note here.

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

**First read `docs/ARCHITECTURE_MACHINE_MODEL.md`** — what runs where, the three
gates, the three container types. It is the only file that answers the plain
question, and it is short. Read it before the six below.

Six files. Under 100 KB total. Read them once, in order, before your first task.

- **`status.md`** (`knowledge/project/`, ~5 KB) — current state, one page.
  **Cadence: updated at strand close, by whoever closes the strand.** If the
  last completed strand isn't reflected in it, that's a bug in the doc.
- **`machine_model.md`** (`knowledge/project/`, ~8 KB) — the six-layer promise
  table. What the machine promises, and how each promise is verified.
- **`design_layers_and_truth_ownership.md`** (`knowledge/project/`, ~24 KB) —
  Pass 2a. The layer ownership map and cross-layer Invariants 1–6.
- **`design_permission_facts_ownership.md`** (`knowledge/project/`, ~28 KB) —
  Pass 2c. Ownership of the four permission facts, artifacts A/B/C/D, the D4
  migration sequence, the D6 test-pin register.
- **`design_permission_bins_shape.md`** (`knowledge/project/`, ~23 KB) —
  Pass 2b. Shape A bins (vocabulary × bin), the `deny<ask<allow` meet, and
  T1/T2/T3 dissolve-only-in-steady-state. The destination.
- **`rulings-permission-facts.md`** (`working_docs/`, ~1 KB) — session rulings,
  append-only. R-1 (network default) is binding at D4 step 4.

**Plus one slot, filled by the operator each session:**

> *The current session handoff, pasted by the operator. Handoffs are
> session-local and are not persisted as files. When you write yours, make it
> a pointer to this document, not a re-derivation of it.*

---

## §2 — Read on question

One line per file: path · size · the question it answers · when to read it.
Read the matching section, not the whole file.

- **`current_map.md`** (`knowledge/project/`, ~3 KB) · *Where am I?* · Read
  when disoriented. One page. **Cadence: phase-boundary.** If a thread isn't
  here, it isn't live.
- **`operator_notes.md`** (`knowledge/project/`) · *Has the operator already said this?* · Grep before asking the operator a question they may have answered. **When picking up a queue item, grep this file for the topic first.**
- **`operator_observations.md`** (`knowledge/project/`, ~78 KB) · *Is this already filed?* · The live ticket queue that `status.md` files its findings into. Read before filing a new bug; newest-at-bottom.
- **`container_controls_findings.md`** (`knowledge/project/`, ~128 KB) · *Container controls, collected (pre-brief).* · Read the matching section, not the whole. Pre-dates `architecture_audit_2026-09.md`.
- **`core.md`** (`knowledge/project/`, ~22 KB) · *Core-feature backlog.* · A workstream queue, not a state doc. Read the section for the strand.
- **`MANDATORY_STOP.md`** (`knowledge/project/`, <1 KB) · *What must I never
  do?* · Read before any action that could trip a stop condition.
- **`open_items_register.md`** (`knowledge/project/`, ~49 KB) · *Is this
  already tracked?* · Read before filing a new item. Sections 1–8; read the
  matching section.
- **`structural_debt_register.md`** (`knowledge/project/`, ~8 KB) · *Is this
  debt known?* · Read before proposing a refactor.
- **`working_agreements.md`** (`knowledge/project/`, ~34 KB) · *What are the
  standing rules?* · **Caveat: dated history mixed with live rules — grep
  `^## ` for the index, read the live rules, skip the dated entries.**
- **`design_container_lifecycle_and_reclamation.md`** (`knowledge/project/`,
  ~26 KB) · *How is a container created, kept, and reclaimed?* · Read when
  working any container strand.
- **`design_intent_state_divergence_family.md`** (`knowledge/project/`,
  ~11 KB) · *Pass-1 edge rulings.* · Read when a fact at two sites disagrees.
- **`system_architecture.md`** (`knowledge/project/`, ~50 KB) · *How is the
  machine built?* · Read when the layer map (2a) isn't enough. Mixed: read the
  "Reference (evergreen)" section; dated sections are historical.
- **`security_architecture.md`** (`knowledge/project/`, ~20 KB) · *How does
  the security layer work?* · Read when working the permission layer.
- **`architecture_audit_2026-09.md`** (`knowledge/project/`, ~140 KB) · *The
  earlier structural audit.* · Read the TOC, then the matching section. The
  R-row table and cross-reference table are the load-bearing parts.
- **`reliability_log.md`** (`knowledge/project/`, ~5 KB) · *Is this flake
  known?* · Read when investigating a recurring test failure.
- **`roadmap.md`** (`knowledge/project/`, ~28 KB) · *Where is this going?* ·
  Read when orienting a strand against the arc. Note: "Current Status" is
  dated 2026-08-16.
- **`v3_roadmap.md`** (`knowledge/project/`, ~9 KB) · *What are the V3
  phases?* · Read when a strand needs its phase context.
- **`v3_vision_full.md`** (`knowledge/project/`, ~8 KB) · *What is V3?* ·
  Read when a strand needs the destination.
- **`gen30_journey.md`** (`knowledge/project/`, ~11 KB) · *How did this arc
  come to be?* · Read for provenance when a decision's origin is unclear.
- **`development_guides.md`** (`knowledge/project/`, ~26 KB) · *How do I
  build, test, and run?* · Read when setting up or running the suite. Read
  the specific topic heading; older sections are archived.
- **`run_design_pass.md`** (`knowledge/project/`, ~43 KB) · *How does a
  design pass work?* · Read before drafting one.

---

## §3 — Pointers (do not cat)

Thirteen files ≥100 KB live under `knowledge/` — 7 under `personal/`, 5 under `archive/`,
and `project/architecture_audit_2026-09.md` at ~140 KB. Grep `^## ` for headers; read
the matching section. The 4 largest (`personal/task_tracker.md`, `personal/lessons_learned.md`,
`personal/bugs_and_fixes.md`, `personal/parking_lot.md`) are the primary offenders — do not `cat`.

- **`lessons_learned.md`** (~450 KB) · Patterns and post-mortems. Read the
  matching section when a pattern repeats.
- **`bugs_and_fixes.md`** (~370 KB) · Filed bugs and their fixes. Read before
  filing a bug that may be a recurrence.
- **`task_tracker.md`** (~460 KB) · The full task history. Read when a
  strand's provenance is unclear.
- **`parking_lot.md`** (~150 KB) · Deferred ideas. Read before treating a
  "new" idea as new.
