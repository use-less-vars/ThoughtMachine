# MAIN_ONBOARDING — the reading index

*You are Main. This file points at what to read. It does not summarize.
Read the files; this only tells you which, and why.*

Your seat is the default one: you ship backend and frontend work in this
repository, and your first duty is to understand the machine you are about to
change. `INFRA_ONBOARDING.md` is the same index for the infrastructure seat —
when the question is about docker, containers, permissions or the host, that
file is the faster path and this one should not be stretched to cover it.

Sizes below are bytes on disk at the time of writing, rounded. They are there so
you can decide *before* you open a file whether you are opening it or grepping it.

## WORKFLOW — read before anything else.

1. Read §1 once at session start, in R order. Do not read §2 speculatively.
2. When a question arises, find the file in §2 that answers it and read that
   file — or, above ~20 KB, the matching section of it. Nothing else.
3. Before acting on a file you have not read this session, run
   `git log --oneline -3 <file>`. The map can be older than the file you are
   about to change.
4. Never `cat` a §3 file whole. Grep `^## ` for the headers, then read the
   section you need.
5. When a fact is in the wrong file, fix it in the file. Never add a note here:
   this file carries pointers, never facts.
6. Enumerate the KB with `find .thoughtmachine/knowledge -name '*.md' -type f`,
   never with `git ls-files`. The KB is gitignored wholesale; `git ls-files`
   will tell you most of it does not exist.
7. Trust the repository over any document, including this one. A document is a
   claim; `git_read`, `ls-tree`, `stat` and `wc` are the evidence.

## §0 — Onboarding ceremony (your first turn)

**What this is.** The first ten minutes of a session, done in a fixed order so
nothing later rests on an assumption nobody checked. **When:** once, at session
start, before any work. **What to read:** nothing yet — you ask the operator for
output first. **What you must be able to answer afterwards:** which branch and
commit am I on; is the running machine built from that commit; what is open;
which files the seat before me left closed and which it left open.

**Turn 0 — restart check.** Ask the operator: *"When was the machine last
restarted?"* If a merge to `dev` landed after the last restart and it touched
runtime code (`web_ui/backend/`, `infra/`, `security/`, `tools/`, `agent/`,
`docker_executor.py`), ask for a restart before proceeding. Otherwise the running
process holds pre-merge modules, tests run against stale code, and findings get
attributed to the wrong change.

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
echo "=== KB tier 1 ===" && \
for f in MANDATORY_STOP.md status.md architecture_invariants.md core.md vision_ledger.md v3_roadmap.md; do \
  p=".thoughtmachine/knowledge/project/$f"; \
  [ -f "$p" ] && echo "yes $(wc -c < "$p") $p" || echo "no  $p"; \
done && \
echo "=== tracked ===" && git ls-tree --name-only HEAD -- .thoughtmachine/knowledge/project/ && \
echo "=== KB tree ===" && find .thoughtmachine/knowledge -name '*.md' -type f | sort
```

**Turn 2 — ask for §1.** One `cat` block for the six tier-1 files. Read them in
R order. `status.md` and `core.md` are the two long ones: read `status.md` from
the top down to the second `## Current state` heading and stop; for `core.md`
read the item you are working and treat the rest as a queue.

**Turn 3 — take the handoff and state your first task.** Restate the verified
state back to the operator in five lines: branch, HEAD, what is closed, what is
open, what you are doing first. If the handoff and `status.md` disagree, say so
before you start work.

That is the whole ceremony: two host blocks, four turns. When a question arises
later, find its file in §2 and read that file — nothing else.

**Two traps this repository sets on a new seat.** (a) A KB file that a tracked
document points at is not automatically tracked: `core.md` is untracked and
gitignored today, so the pointer outlives the file on any fresh clone. The
`git ls-tree` line above exists for exactly that. (b) `personal/lessons_learned.md`
and `project/lessons_learned.md` are two different files. The first is a large
historical dump of post-mortems; the second is short, tracked, and generation-
scoped. Read the project one by default.

---

## §1 — Mandatory (read once)

**What this tier is.** The six files that decide what you may change and what is
true right now. Read them once, in R order, before your first task; nothing in §2
substitutes for any of them. **When:** session start, then R2 and R5 again at
every strand close. **What to read:** all six rows below. **What you must be able
to answer afterwards:** what must I never do (R1); what is true now and what
closed last (R2, R5); which property I may not break (R3); which pending core
work already exists, and under which C-ID (R4); which V3 phase this belongs to (R6).

| ID | file | why | time |
|---|---|---|---|
| R1 | `.thoughtmachine/knowledge/project/MANDATORY_STOP.md` | Settles the stop conditions and the three registry guards. You can answer: what do I refuse to touch without an explicit ruling? | 2 min |
| R2 | `.thoughtmachine/knowledge/project/status.md` | Settles current state: the newest `## Current state` block, what closed, what the last handoff left open. You can answer: what is true now, and what was true at the previous close? | 8 min |
| R3 | `.thoughtmachine/knowledge/project/architecture_invariants.md` | Settles the properties the system must keep across layers. You can answer: which invariant does my change touch? | 8 min |
| R4 | `.thoughtmachine/knowledge/project/core.md` | The single home for pending core work (C-items), a workstream queue, not a state doc. You can answer: is the thing I am about to build already queued, and under which C-ID? | 10 min |
| R5 | `.thoughtmachine/knowledge/project/vision_ledger.md` | The five standing commitments V1–V5 and, per item, whether the machine is currently keeping them. You can answer: which commitment does my change strengthen or weaken? | 8 min |
| R6 | `.thoughtmachine/knowledge/project/v3_roadmap.md` | Settles the V3 phases. You can answer: which phase is this strand in, and what has to be true before the next one? | 6 min |

**Reading order is R1 → R6.** R1 first because it is short and vetoes work; R2
and R5 last because they are the two you will re-read weekly.

---

## §2 — Read on question

**What this tier is.** One row per file: the question it answers and when to read
it. A lookup table, not a curriculum. **When:** continuously, one file at a time,
when a question arises. **What to read:** the matching row, then the matching
section of that file — anything above ~20 KB, grep the headers first. **What you
must be able to answer afterwards:** the specific question that sent you here,
sourced to a file you can name.

| ID | file | why | time |
|---|---|---|---|
| R7 | `.thoughtmachine/knowledge/project/machine_model.md` | The six-layer promise table: what the machine promises and how each promise is verified. Read when a fix needs its promise named. | 6 min |
| R8 | `.thoughtmachine/knowledge/project/current_map.md` | *Where am I?* One page, phase-boundary cadence. If a thread is not here it is not live. | 3 min |
| R9 | `.thoughtmachine/knowledge/project/KB_MAP.md` | Which file owns which question. Read when this index does not obviously answer it. | 3 min |
| R10 | `docs/ARCHITECTURE_MACHINE_MODEL.md` | What runs where, the three gates, the three container types. Short, and the only file that answers the plain question. Read before touching runtime code. | 8 min |
| R11 | `docs/CAPABILITIES.md` | What the product can do today, capability by capability. Read before claiming something is missing or already supported. | 8 min |
| R12 | `docs/KB_POLICY.md` | What belongs in the KB, what may be tracked, and the tier policy. Read before adding or re-filing a KB document. | 6 min |
| R13 | `.thoughtmachine/knowledge/project/lessons_learned.md` | Gen 60, "The Heat of the Moment": the cost of trusting a report over an artifact. Read before repeating a shape. | 6 min |
| R14 | `.thoughtmachine/knowledge/project/v3_vision_full.md` | What V3 is. Read when a strand needs its destination rather than its phase. | 6 min |
| R15 | `.thoughtmachine/knowledge/project/roadmap.md` | Where this is going, the long arc. Note that its "Current Status" section is dated 2026-08-16. | 8 min |
| R16 | `.thoughtmachine/knowledge/project/system_architecture.md` | How the machine is built, when the layer map in R10 is not enough. Read the "Reference (evergreen)" section; dated sections are history. | 12 min |
| R17 | `.thoughtmachine/knowledge/project/security_architecture.md` | How the security layer works. Read when working anything in the permission layer. | 10 min |
| R18 | `.thoughtmachine/knowledge/project/working_agreements.md` | The standing rules between seats. Dated history is mixed with live rules: grep `^## `, read the live ones, skip the dated. | 10 min |
| R19 | `.thoughtmachine/knowledge/project/development_guides.md` | How to build, test and run. Read the specific topic heading; older sections are archived. | 10 min |
| R20 | `.thoughtmachine/knowledge/project/operator_observations.md` | The live ticket queue that `status.md` files findings into. Grep for the topic before filing a new bug; newest at the bottom. | grep |
| R21 | `.thoughtmachine/knowledge/project/operator_notes.md` | *Has the operator already said this?* Grep before asking a question they may have answered. | grep |
| R22 | `.thoughtmachine/knowledge/project/open_items_register.md` | *Is this already tracked?* Sections 1–8; read the matching section before filing a new item. | grep |
| R23 | `.thoughtmachine/knowledge/project/structural_debt_register.md` | *Is this debt known?* Read before proposing a refactor. | 5 min |
| R24 | `.thoughtmachine/knowledge/project/reliability_log.md` | *Is this flake known?* Read when a test failure looks familiar. | 4 min |
| R25 | `.thoughtmachine/knowledge/project/ideas_and_brainstorming.md` | Deferred ideas. Read before treating a "new" idea as new. | 5 min |
| R26 | `.thoughtmachine/knowledge/project/operator-dashboard.md` | The operator-facing dashboard notes. Read when a number rendered on screen is disputed and you need where it comes from. | 8 min |
| R27 | `.thoughtmachine/knowledge/project/hall_of_fame.md` | What good work looked like here, in short. Read before writing a close-out. | 3 min |
| R28 | `.thoughtmachine/knowledge/project/gen30_journey.md` | Provenance. Read when a decision's origin is unclear and you are tempted to call it arbitrary. | 8 min |
| R29 | `.thoughtmachine/knowledge/project/run_design_pass.md` | How a design pass works. Read before drafting one. | 12 min |
| R30 | `.thoughtmachine/knowledge/project/INFRA_ONBOARDING.md` | The infra seat's index: docker, containers, permissions, host. Read when the question is infrastructure rather than product. | 8 min |
| R31 | `docs/system-map.md` | Where things live in this tree. Read before your first search, not after it fails. | 8 min |
| R32 | `docs/docker_usage.md` | How things are run inside containers. Read before using a runner for real work. | 5 min |
| R33 | `docs/architecture/thoughtmachine_architecture_baseline.md` | The architecture baseline the later passes build on. Read when R10's summary raises a "why is it like this". | 8 min |
| R34 | `docs/architecture/config_ownership.md` | Who owns which configuration fact. Read when a config value has two contenders. | 6 min |
| R35 | `docs/architecture/session_history_bounding.md` | How session history is bounded and what that costs. Read when working anything that renders session counts. | 6 min |
| R36 | `docs/architecture/wlm_completion.md` | The worker lifecycle design as completed. Read before changing dispatch or lifecycle. | 8 min |
| R37 | `docs/architecture/vault-restructuring.md` | The vault layout decision. Read before touching vault paths. | 4 min |
| R38 | `docs/decisions/logging-architecture.md` | The logging decision and its shape. Read before adding a log surface. | 5 min |

---

## §3 — Pointers (do not cat)

**What this tier is.** Files that are large, historical or personal. They are real
and sometimes the answer, but reading one whole is a way to lose a session.
**When:** last, after §1 and §2 have failed to answer the question. **What to
read:** `grep -n '^## '` for the headers, then the matching section — never the
whole file. **What you must be able to answer afterwards:** nothing. This tier is
for provenance and precedent, not for obligations.

| ID | file | why | time |
|---|---|---|---|
| R39 | `.thoughtmachine/knowledge/project/architecture_audit_2026-09.md` | The earlier structural audit. Read its TOC, then one section. The R-row table and the cross-reference table are the load-bearing parts. | grep |
| R40 | `.thoughtmachine/knowledge/project/container_controls_findings.md` | Container controls collected before the audit above. Read the matching section. | grep |
| R41 | `docs/agent-core-spec.md` | The agent core specification. Reference only: grep an identifier, read around it. | grep |
| R42 | `.thoughtmachine/knowledge/project/archive/status-archive-gen48-55.md` | The closes before Gen 56, in full. Read one close when a modern claim cites it. | grep |
| R43 | `.thoughtmachine/knowledge/personal/task_tracker.md` | The full task history. Read when a strand's provenance is unclear. | grep |
| R44 | `.thoughtmachine/knowledge/personal/lessons_learned.md` | The long post-mortem dump — a different file from R13, and much larger. Read the matching generation when a pattern repeats. | grep |
| R45 | `.thoughtmachine/knowledge/personal/bugs_and_fixes.md` | Filed bugs and their fixes. Read before filing a bug that may be a recurrence. | grep |
| R46 | `.thoughtmachine/knowledge/personal/parking_lot.md` | Deferred ideas, unfiltered. Read before treating a "new" idea as new. | grep |
| R47 | `.thoughtmachine/knowledge/personal/handoffs.md` | Past session handoffs. Read the last two when the current handoff assumes context you do not have. | grep |
| R48 | `.thoughtmachine/knowledge/personal/worker_notes.md` | What workers learned about their own runtime. Read before designing a worker brief. | grep |
| R49 | `.thoughtmachine/knowledge/archive/` | Eight archive files, several hundred KB, historical. Read one section for one fact. | grep |
| R50 | `.thoughtmachine/knowledge/project/brain_pour_2026-10-21.md` | One long dump from a single session. Dated: read for provenance, never for current truth. | grep |

---

## What this seat must NOT do

1. **Do not weaken the three registry guards** — `_quarantine`, the empty-scan
   `RuntimeError`, and the deliberate absence of auto-rebuild. They are why a
   damaged registry fails loudly instead of quietly rebuilding itself around the
   damage. Removing one is not a cleanup; it is deleting the alarm.
2. **Do not run `vault_repair --apply`, do not run `vault_gc`, and do not write
   anything under `~/.thoughtmachine`.** The vault is the operator's. This seat
   reads it.
3. **Do not treat a worker's report as evidence.** A worker report is a claim
   about a process you cannot see, and it arrives through a dispatch path that
   has its own defects (R5, V1). Verify with the orchestrator's own tools —
   `git_read`, `ls-tree`, `stat`, `wc` — before anything is committed.
4. **Do not call a document delivered until it is in `git ls-tree`.** `git
   status` will happily call a new KB file fine while it is absent from the tree,
   because the KB is gitignored wholesale and only explicit `!` negations track
   anything.
5. **Do not assume a file under `knowledge/project/` is tracked because the KB
   points at it.** `core.md` — the pending-core queue, C-items, 39.6 kB — is
   untracked and gitignored today, so every tracked document that names it
   points at a file that will not survive a clone. Confirm with
   `git ls-tree <branch> <path>` before you build on a file's existence.

Maintained with status.md, vision_ledger.md and core.md. If this file and the repository disagree, the repository wins.
