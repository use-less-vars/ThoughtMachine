> **CURRENT-STATE BANNER.** `project/vision_ledger.md:24` (V1, Gen 60/61, 2026-11) records a dated **BROKEN** verdict on the dispatch envelope: `meta` is dropped, a long `query` is truncated (→ hallucination), and `timeout_triggered` arrives contradicting correct content.
> Read Theme H (AW-201 … AW-207) as **current**, not historical. This archive documents a live defect class that the ledger independently confirms, and the archive's own dates run to 2026-11-03.
> The same applies to the git read surface (Theme L): accepted-and-ignored parameters are still producing wrong answers, not errors, as of the last recorded case.

# Agent / Worker Problems — Part 3: Archive Harvest (AW-201 →)

**Scope.** Part 1 (`part1_kb.md`, AW-001..AW-100, themes A–G) and Part 2 (`part2_workingdocs.md`,
AW-101..AW-200) covered the *live* knowledge base. Part 1 explicitly listed a set of archive files it
did **not** read; this part covers exactly those, plus two extras. Nothing here duplicates a Part 1
mechanism: where a mechanism overlaps, the item is a *distinct* mechanism and Part 1's ID is cited
instead of the mechanism being re-described.

**Read-only.** This report is the only artifact written. No repository file, KB file or git state was
modified (no `git_write`, no `KnowledgeBaseTool` write, no edits outside this file).

**Citation convention.** Two archive corpora dominate, so they are abbreviated:

| Label | File |
|---|---|
| `LL-10-29:Lnnn` | `.thoughtmachine/knowledge/personal/lessons_learned_archive_2026-10-29.md` |
| `LL-11-05:Lnnn` | `.thoughtmachine/knowledge/personal/lessons_learned_archive_2026-11-05.md` |
| `BF:Lnnn` | `.thoughtmachine/knowledge/personal/bugs_and_fixes.md` |
| `VL:Lnn` | `.thoughtmachine/knowledge/project/vision_ledger.md` |

Line numbers are the archive's own (both archives prepend a 4-line notice, so *archive line =
original line + 4*). Every evidence row quotes the archive's actual heading fragment or date.

---

## 1. FILE INVENTORY — what was scanned, what was skipped, and why

Corpus = 13 files, ≈2.1 MB / 13,600 lines. Whole-file reads are impossible inside a worker budget
(`BF:L1-5` and the 11-05 archive both carry explicit "DO NOT READ THIS FILE WHOLE" banners; `BF` alone
is ~100k tokens). Method was therefore **bounded range reads + heading/keyword greps**, with the
heaviest files swept rather than read.

| # | File (`.thoughtmachine/knowledge/`) | Bytes | Lines | Treatment | Coverage |
|---|---|---|---|---|---|
| 1 | `personal/lessons_learned_archive_2026-10-29.md` | 569,256 | 2,671 | ~14 targeted ranges read (headings + bodies) | ~35–40% of body, ~70% of `^## ` headings (393 total) |
| 2 | `personal/lessons_learned_archive_2026-11-05.md` | 86,706 | 598 | L150–L598 read (contiguous); L1–L149 index only | ~85% of body |
| 3 | `personal/bugs_and_fixes.md` | 348,212 | 2,211 | heading + keyword grep (`git_read`, `GitWriteTool`, `GitInfoTool`, `worker_name is required`); head L1–L30 read for format | ~10% of body, biased to the tooling entries (newest third) |
| 4 | `personal/bugs_and_fixes_archive_2026-11-05.md` | 235,887 | 934 | heading grep only | ~5% (headings matched: 0 tooling-specific; see Limitations) |
| 5 | `personal/task_tracker.md` | 103,426 | 878 | heading + keyword grep (`Worker(action=`, `spawn`, `soft-timeout`) | ~5% (matches are S-wave *design* logs, not harness defects) |
| 6 | `personal/task_tracker_archive_2026-10-29.md` | 522,853 | 2,059 | heading grep only | <2% (298 `## ` headings; dominated by implementation logs) |
| 7 | `personal/handoffs.md` | 76,451 | 829 | head + heading grep | ~5% |
| 8 | `personal/agent_worker_timeline.md` | 107,848 | 776 | heading sweep attempted — **failed** (headings do not match `^## `; see Limitations) | ~2% |
| 9 | `project/lessons_learned.md` | 12,392 | 99 | small; heading grep | ~50% |
| 10 | `project/open_items_register.md` | 63,868 | 557 | head read — carries an explicit **SUPERSEDED (2026-10-21, Gen 47) — DO NOT ROUTE WORK FROM THIS FILE** banner | banner + structure |
| 11 | `project/structural_debt_register.md` | 8,025 | 118 | heading grep | ~40% |
| 12 | `project/working_agreements.md` | 85,035 | 835 | keyword grep (`timeout_seconds`, `worker_name`, `soft-timeout`, `wall clock`) | ~10% |
| 13 | `project/status.md` | 39,630 | 316 | head + keyword grep | ~15% |
| + | `project/vision_ledger.md` (not on Part 1's list; found by keyword grep) | 88 lines | small | **L10–L30 read in full** — carries a dated *verdict* on the dispatch family | 100% of V1 |

**Yield.** 65 items (AW-201..AW-268; IDs AW-208/209/259 and others in the gaps are unused — see the
per-theme counts and the status table in §4) drawn
overwhelmingly from #1 and #2. #3 contributed 5 items; #13/#12/#10/#11/#5 contributed **zero**
worker-harness defects (they are design/process/status documents — recorded here as a negative result
so a later pass does not re-spend the budget on them).

**Single most useful find.** `VL:L24` — a *current* (Gen 60/61, 2026-11) operator-facing verdict that
the dispatch-envelope family is **BROKEN**, which converts several 2026-09 "lessons" from historical
notes into live defects. Cited throughout as `VL:L24`.

---

## 2. ITEMS BY THEME

### THEME H — DISPATCH ENVELOPE & BRIEF DELIVERY (what the worker actually received)

**AW-201 — `context["briefing"]` is silently dropped on spawn**
- **What happens:** a `briefing` key passed to `Worker(action="spawn")` never reaches the worker; only
  `query` arrives. The system prompt calls `briefing` "strongly recommended". One spawn received a
  5-path brief with 4 paths inlined in `query` and the 5th only in `briefing`; the worker got a 4-item
  task and correctly stopped to ask.
- **Evidence:** `LL-11-05:L474` "`context["briefing"]` is NOT delivered to the worker"; repeated
  `LL-11-05:L486` ("silently dropped on spawn"; a briefing-only instruction "do not touch git" would not
  have arrived) and `LL-11-05:L527` (5-path brief arrived as 4-item task, ~60 s round trip for zero
  work). All three dated **2026-11-03**. Adjudicated at `VL:L24` under V1 *"the envelope carries what it
  says it carries"* — **Verdict: BROKEN**.
- **Recurrence:** 3 recorded dispatches, 3 generations (Gen 57, W8 P3-S0, Gen 57 opening) — 0 successes
  recorded once it was being watched.
- **Impact:** causes wrong output (worker answers a brief it only half has).
- **Suggested fix:** inline everything in `query`, treat `briefing` as decoration (`LL-11-05:L527`);
  root fix is named at `VL:L24` — deliver `context["meta"]`/`briefing` or stop advertising them.
- **Status:** **LIVE** (`VL:L24`, Gen 60/61, 2026-11).

**AW-202 — Long inline `query` payloads are truncated; the worker confabulates the rest**
- **What happens:** a long single-key `query` is cut off in transit. The worker answers the half it saw
  and invents the remainder — and the defect is invisible from the orchestrator's side, because the
  worker's answer is confident and on-topic.
- **Evidence:** `VL:L24` "Long inline `query` payloads are truncated — a worker answered a brief it had
  only half of and hallucinated the rest" (Gen 60/61, 2026-11); earlier same-mechanism observation
  `LL-10-29:L1223(1)` (2026-09-20) "~10k-char briefing pasted into worker context was truncated; worker
  reconstructed its own deliverable".
- **Recurrence:** 2 documented losses (2026-09-20, 2026-11) — and this is the §1 blind spot: a truncated
  query is indistinguishable from a short one at the call site, so the true count is a lower bound.
- **Impact:** causes wrong output.
- **Suggested fix:** standing mitigation (in force at `VL:L24`): "park every payload longer than a few
  lines in a `WorkingDocument` and send a short query naming the `doc_id`".
- **Status:** **LIVE** (`VL:L24`). Distinct from Part 1 **AW-001** (agent-side long *prompt* loss →
  fabrication): AW-202 is the worker-side envelope; both must be fixed at the envelope.
- **Note:** Part 1's AW-001 and this item share a *root cause family*, not a mechanism.

**AW-203 — `Worker spawn` drops `context["meta"]`**
- **What happens:** the `meta` block of the spawn context is dropped, so a worker never sees the
  structured metadata its brief declares.
- **Evidence:** `VL:L24` "`Worker spawn` drops `context["meta"]`" (Gen 60/61, 2026-11).
- **Recurrence:** 1 explicit (bundled with V1's BROKEN verdict; not separately instrumented).
- **Impact:** causes wrong output.
- **Suggested fix:** none recorded beyond the V1 verdict.
- **Status:** **LIVE** (`VL:L24`).

**AW-204 — Spawn rejects an `{"arguments": …}` envelope; only flat named params work**
- **What happens:** `Worker(action="spawn")` requires flat named parameters (`action`, `worker_name`,
  `context`); the JSON-schema-shaped `{"arguments": {...}}` form fails.
- **Evidence:** `LL-11-05:L378` (2026-11-02, "Cost one wasted spawn attempt") and `LL-11-05:L426`.
- **Recurrence:** 2 (2026-11-02 ×2).
- **Impact:** costs significant time (one wasted dispatch on first use).
- **Suggested fix:** none recorded (documented as a trap, not fixed).
- **Status:** **LIVE**.

**AW-205 — `worker_name` is required although the schema and system prompt say it is optional**
- **What happens:** `spawn` fails with `{"error": "worker_name is required for action 'spawn'"}` while the
  tool schema marks `worker_name` optional and the system prompt states "there is a default worker,
  `worker_name` not needed". Either the schema or the validator is wrong; the error surfaces only after
  the call is constructed, so it costs a full round trip.
- **Evidence:** `LL-11-05:L455` "either the schema doc or the validator is wrong. Cost: one wasted
  roundtrip on the first dispatch of every session" and `LL-11-05:L557` (both **2026-11-03**);
  `LL-11-05:L426` (2026-11-02).
- **Recurrence:** 3 in the 11-05 archive alone; 6+ cumulative (Part 1 **AW-005** already recorded it as
  permanent friction).
- **Impact:** costs significant time (once per session, minimum).
- **Suggested fix:** make the default worker genuinely implicit, or drop the "optional" claim from the
  schema/prompt — one of the two must change (`LL-11-05:L455`).
- **Status:** **LIVE**. Cross-reference: Part 1 **AW-005**; recurrence now 6+ and still unreconciled.

**AW-206 — Large exact-match replacement payloads are lost mid-task; the assert-count script is the fix**
- **What happens:** a 21-item (~6 KB) exact-match replacement payload sent to a worker failed **twice**
  (once the worker self-summarised mid-task and lost the payload, once it misdiagnosed the situation).
  What worked: split to ≤4 KB per spawn and instruct the worker to write a Python script holding
  `(old, new, expected_count)` tuples that asserts `text.count(old) == expected` and **aborts without
  writing** on mismatch.
- **Evidence:** `LL-11-05:L183` heading "**Bulk doc edits: small anchor payloads + assert-count
  scripts (Gen 54)**", dated **2026-10-29**. Both split passes completed ~22 s / 1 tool call each.
- **Recurrence:** 2 failures + 2 successes (same payload, same day) — a clean A/B.
- **Impact:** costs significant time (two full dispatches burnt) and risks wrong output (bending the file
  to fit bad arithmetic).
- **Suggested fix:** the assert-count script pattern above; also "forbid `cat`/`head` of the target — the
  strings are exact, the worker never needs to read it"; verify with `git_read diff` rather than the
  worker's summary.
- **Status:** **LIVE** (method recorded, harness unchanged).

**AW-207 — Orchestrator handoffs themselves carry wrong facts (handoff drift)**
- **What happens:** a handoff document supplied a filename that did not exist; a worker probe caught it.
  The same archive shows the *inverse* paying off: an engineer's brief audited against the tree before
  dispatch yielded 5 substantive errors, and a follow-up instruction produced a **sixth** hallucinated
  attribute.
- **Evidence:** `LL-10-29:L496(6)` (2026-08-25) "handoff carried a wrong filename caught by worker
  probe"; `LL-11-05:L539-548` heading "**S3 brief verification (Gen 57 brief vs code) — 5 substantive
  errors found before any code was written**" (2026-11-03) — e.g. `_pause_event` "does not exist anywhere
  … (0 occurrences)", design-doc line numbers stale by 100–250 lines, §-number collisions across docs;
  and `LL-11-05:L560` "**Sixth hallucinated attribute in two briefs: `_runtime_sock`**" (0 hits).
- **Recurrence:** 3 documented (2× handoff/brief wrong, 1× sixth-attribute) across 2026-08-25 →
  2026-11-03.
- **Impact:** blocks work (a brief's phantom symbol becomes a predicate the implementation cannot
  satisfy).
- **Suggested fix:** audit the brief against the tree before dispatch — "a brief is a claim set, not a
  spec" (`LL-11-05:L588`); 7 read-only probes cost 104 s and falsified 3 of 5 items; grep for *call
  sites*, not definitions (`LL-11-05:L594`).
- **Status:** **LIVE** (the practice is the mitigation; nothing prevents the bad brief).

---

### THEME I — WALL CLOCK, BUDGET AND KILL SEMANTICS

**AW-210 — Worker wall clock is hard-capped at ~150 s regardless of `timeout_seconds`**
- **What happens:** `timeout_seconds=900` and `timeout_seconds=150` produce the *same* kill. The
  parameter is accepted and silently inert above the cap. Two calls ended at 150.2 s and 164 s with
  `timeout_triggered: true`. Consequence: a job that writes a 250-line file and then runs a 14 s suite
  hits the wall mid-write.
- **Evidence:** `LL-11-05:L523` heading "**Worker wall clock is hard-capped ~150 s regardless of
  `timeout_seconds`**", dated **2026-11-03**.
- **Recurrence:** 3+ measured kill points in that session.
- **Impact:** causes wrong output (mid-write truncation) and blocks work (long jobs impossible).
- **Suggested fix:** "Budget per call, do not plan 'one big final step'" (`LL-11-05:L525`).
- **Status:** **LIVE**. Note the contradiction with AW-212/AW-215 — the cap is *not one number*.

**AW-211 — The cap shrinks within and across sessions (350 s → 210 s → ~150 s)**
- **What happens:** field notes for the same repo report "The per-dispatch wall is ~350 s at best and
  shrinks (I saw 210 s and then ~150 s)". An overrun is killed **and its container is reclaimed**.
- **Evidence:** `LL-11-05:L356` heading "## Dispatching the default worker on this repo (wave-7 field
  notes)" §"Traps learned the hard way", dated **2026-11-02**.
- **Recurrence:** 3 distinct walls in one wave.
- **Impact:** costs significant time (every plan must be re-budgeted mid-session).
- **Suggested fix:** none recorded — the note itself says anything long-running must go to the
  background, which AW-217 then shows does not survive.
- **Status:** **LIVE**.

**AW-212 — A second, much larger and equally undocumented cap (~600 s) governs tool time**
- **What happens:** measured 628 s elapsed on a run that asked for 900 s, status `timeout`, tools
  restricted to `Respond`+, mid-sentence. Elsewhere "the worker dies ~165–200 s wall", "soft cap ~200 s",
  a "660 s ceiling variant", and a `timeout_seconds=145` spawn that ran 470 s and returned
  `status=final, timeout_triggered=false`.
- **Evidence:** `LL-11-05:L420-422` heading "### Worker dispatch caps (measured, not documented)"
  (2026-11-02); `LL-11-05:L379` (2026-11-02, 145 → 470 s, no trigger); `LL-11-05:L463` (2026-11-03);
  `LL-10-29:L340/343` "timeout mechanics CONFIRMED" (2026-08-07), `L712` (2026-09-03), `L1033`, `L1508`
  (2026-09-23).
- **Recurrence:** 8+ independent measurements at 150 / 165–200 / 200 / 350 / 420 / 470 / 600 / 660 / 960 s.
- **Impact:** blocks work — no caller can derive a safe budget from the documented surface.
- **Suggested fix:** "Budget by chunk size, not by that field" (`LL-11-05:L379`); document the cap.
- **Status:** **LIVE** (evidence across 2026-08-07 → 2026-11-03).

**AW-213 — `DockerCodeRunner` has its own hard ~420 s wall cap, and the harness fires before `timeout`**
- **What happens:** the full suite (≈6,042 tests) cannot be run in one command: pytest was at 74 % when
  the runner killed the container — `exit_code: -2, timed_out: true, duration: 420.455s`, **empty
  stdout**, shell never reached the trailing `echo`. A `timeout 360` wrapper does not help because the
  harness fires first.
- **Evidence:** `LL-11-05:L576` §5 "TOOLING DEFECT — DockerCodeRunner has a hard ~420 s wall cap per
  call" (2026-11-03); corroborated `LL-10-29:L1209-1211` (2026-09-21).
- **Recurrence:** 2 (2026-09-21, 2026-11-03).
- **Impact:** blocks work (full-suite verification is not expressible in one call).
- **Suggested fix:** "slice the suite by test file across several calls" (`LL-11-05:L576`).
- **Status:** **LIVE**.

**AW-214 — `ContainerExecTool`'s ~30 s default timeout kills the container**
- **What happens:** the tool's own default is ~30 s; when it fires the container is **killed**
  (`exit -2`, "container is not running"), not merely the command. Long greps and test runs must pass an
  explicit large timeout, and recovery needs `ContainerStartTool(timeout=600)`.
- **Evidence:** `LL-11-05:L380` (2026-11-02).
- **Recurrence:** 1 explicit (a design default that guarantees the failure for any real grep/test).
- **Impact:** costs significant time (container restart per overrun).
- **Suggested fix:** pass explicit large `timeout`; recover with `ContainerStartTool(timeout=600)`.
- **Status:** **LIVE**.

**AW-215 — Soft timeout returns `timeout_triggered: true` beside complete, correct content**
- **What happens:** a spawn reports `status: timeout` / `timeout_triggered: true` with output that is
  finished, or reports `elapsed_seconds` far below the configured `timeout_seconds` (e.g. `elapsed 421.6`
  on a `timeout_seconds: 600` run whose content was correct; `spawned:false, timeout_triggered:true` at
  129 s against 240 s requested).
- **Evidence:** `VL:L22-24` (V1 "How you would know it is broken" + **Verdict: BROKEN**, Gen 60/61,
  2026-11); `LL-11-05:L379`; `LL-10-29:L1278` (2026-09-21).
- **Recurrence:** 3+ (2026-09-21 → 2026-11).
- **Impact:** causes wrong output — a re-run of an already-complete job destroys or duplicates work.
- **Suggested fix:** none recorded in code; the rule derived is "check content before re-running"
  (`LL-10-29:L1278`).
- **Status:** **LIVE** (`VL:L24`).

**AW-216 — A worker that spends its budget investigating returns `timeout` with ZERO artefacts**
- **What happens:** asked to both research and write, the worker produced "excellent research and no
  files". The working split is one worker researches and **reports**, a second (or a follow-up `query`
  with "write now, do not re-research, max 10 tool calls") writes. `meta.not_done` /
  `meta.needs_direction` made it recoverable.
- **Evidence:** `LL-11-05:L463` (2026-11-03); budget-aware prompting `LL-11-05:L465`: "telling a worker
  'AT MOST N tool calls for grounding, then write' changed a timeout into a delivered artefact".
- **Recurrence:** 2 (research/write bundling, plus the prompt fix's single confirmed conversion).
- **Impact:** costs significant time (whole dispatch wasted).
- **Suggested fix:** as recorded — split research from writing; budget-aware prompt clause.
- **Status:** **LIVE** (pattern-level mitigation only).

**AW-217 — Backgrounded work does not survive: the process dies with the container and `/tmp` is wiped**
- **What happens:** "backgrounding (`nohup` + poll) does not survive — the process either becomes a
  zombie or the container is recycled and `/tmp` is wiped". Symptom of a lost run: no summary line, or
  the log stops at ~64 % with no F/E. A whole 400 s full-suite run was lost this way.
- **Evidence:** `LL-11-05:L337` heading "**Running the whole suite behind a ~180s dispatch wall (W6)**"
  (2026-11-02); `LL-11-05:L356-357` ("`/tmp` dies with the container, which cost a whole 400 s
  full-suite run. Use e.g. `<worktree>/.gate.log`"); `LL-11-05:L384`; `LL-10-29:L1001` (2026-09-16),
  `L730`.
- **Recurrence:** 4 (2026-09-16 → 2026-11-02).
- **Impact:** costs significant time; and a "green" claim resting on a backgrounded run is unfounded.
- **Suggested fix:** write logs to a mounted workspace path, not `/tmp`; never claim green from a
  backgrounded run.
- **Status:** **LIVE**. (AW-217 vs AW-211's "go to the background" advice is the contradiction that
  makes this family worth naming.)

**AW-218 — The full suite (~6,000 tests) cannot be honoured inside a worker call; it needs ~8 dispatches**
- **What happens:** "full suite collects 6,042 tests (`--collect-only` alone = 9.6 s), so 'run the full
  suite before commit' cannot be honoured inside a ~150 s worker call". The working plan is ~8
  sequential **foreground** chunk dispatches of ≤~700 tests each (measured ~10–11 tests/s but wildly
  uneven: 470 tests in 18 s vs 1,901 in 177 s).
- **Evidence:** `LL-11-05:L550` (2026-11-03); `LL-11-05:L339` (2026-11-02) with the chunk plan;
  `LL-11-05:L337`.
- **Recurrence:** 3 plans (2026-11-02, 2026-11-03 ×2).
- **Impact:** costs significant time (verification becomes a multi-dispatch project).
- **Suggested fix:** the chunk plan as recorded + "only run a second command if the first was well under
  ~90 s"; `date +%s` because `time` is not installed.
- **Status:** **LIVE**.

**AW-219 — At ~89k tokens the worker goes token-critical and loses all tools; `force=True` is the recovery**
- **What happens:** above the threshold only `Respond` is available. A fresh `force=True` spawn finished
  the interrupted task in 65 s / 7 tool calls.
- **Evidence:** `LL-11-05:L423-425` (2026-11-02).
- **Recurrence:** 2 (this, plus `LL-11-05:L529` mid-turn restriction leaving a file unimportable).
- **Impact:** costs significant time; AW-221 shows the recovery is itself unsafe.
- **Suggested fix:** `force=True` spawn (recorded) — but see AW-221 for the context-inheritance trap.
- **Status:** **LIVE**.

---

## THEME J — Instance lifecycle, spawn semantics, and identity (AW-220 … AW-227)

The dispatch path is *stateful*, and every state transition is under-specified. A spawn does not reliably produce a new worker; a stop does not reliably free an instance; the status string the orchestrator keys on does not belong to a documented vocabulary. The orchestrator therefore believes it owns a fresh worker and is in fact talking to an old one — intermittently, silently, and with plausible-looking output.

**AW-220 — `spawn` reuses a running instance and silently drops the query**
- What: Calling spawn for a worker name that already has a live instance attaches to the existing instance instead of minting a new one; the new `query` is discarded. The next worker reply is the *previous* query's answer, so the orchestrator reads a correct answer to the wrong question.
- Evidence: LL-10-29:L506 (2026-08-26, live test: two spawns, one instance, one answer), LL-10-29:L1403 (2026-09-22), LL-10-29:L1407.
- Recurrence: 3 (2026-08-26 → 2026-09-22).
- Impact: **Wrong output, no error.** The failure is invisible at the boundary — the orchestrator books "answer N" against "query N+1", permanently one query behind, and every downstream conclusion built on that answer is built on the previous turn's context.
- Fix: Reject `spawn` on a live name with an explicit error; require `stop` first, or force `force=True` semantics to *document* that context is inherited (see AW-221).
- Status: **LIVE**.

**AW-221 — `force=True` inherits the predecessor's context and can be born tool-restricted**
- What: The escape hatch that is supposed to make spawn "fresh" instead reloads the predecessor's conversation, so the new instance starts carrying an earlier instance's mistakes, decisions and (once) its permission restrictions.
- Evidence: LL-10-29:L1569 (2026-09-24), LL-10-29:L1223 item (3) (2026-09-20).
- Recurrence: 2 (2026-09-20 → 2026-09-24).
- Impact: Blocks work: the worker cannot be given a clean slate, and a stale permission footprint follows it into the new instance — the operator has no way to inspect or reset it.
- Fix: None recorded. `force` should mean "new instance, empty context"; if it cannot, it must not be named `force`.
- Status: **LIVE**.

**AW-222 — Stale instances block spawning; the only cure is to stop *both* namespaces**
- What: After a few spawns, `spawn` returns `{"status": "stopped"}` and the worker executes **zero** tool calls, even with `force=True`. The reliable procedure is to `stop` `default` **and** `default2`, then spawn. Instances are not reaped when their work ends: 5 idle instances had accumulated after 4 spawns.
- Evidence: LL-10-29:L1032 (2026-09-16), LL-10-29:L471-473 (2026-08-24, "no auto-reap, 5 idle after 4 spawns").
- Recurrence: 2 (2026-08-24 → 2026-09-16), plus every session where the workaround had to be applied.
- Impact: Blocks work. Worse, the failure shape is a *success-shaped* reply (`status: stopped` returns without raising), so a loop that spawns and waits looks like a slow worker rather than a dead one.
- Fix: Reap on completion; make spawn fail loudly on a name collision; treat `stopped` as an error, not a status.
- Status: **LIVE**.

**AW-223 — A second namespace (`default2`) is useful for genuine parallelism but can read half-written files**
- What: Running two workers concurrently (`default` + `default2`) is the only parallelism available, and it works — but a second worker reading a file the first is mid-write sees a torn, half-written file, and reports it as content.
- Evidence: LL-11-05:L558 (2026-11-03).
- Recurrence: 1.
- Impact: Wrong output under exactly the condition parallelism is used for.
- Fix: Serialise writes through the orchestrator, or require the writer to signal completion before a reader is dispatched on the same path.
- Status: **LIVE**.

**AW-224 — An overrun puts the instance in a Respond-only state that survives one spawn**
- What: After a timeout/overrun the worker's tool surface collapses to `Respond` alone. The restriction persists across a subsequent spawn and only clears on the *next* query. In the recorded case a worker was forced to deliver a result through `Respond` with envelope `final` + `confidence: high` while the restriction, not its evidence, dictated the shape.
- Evidence: LL-11-05:L358 (2026-11-02), LL-10-29:L489-491 and L494 (2026-08-25).
- Recurrence: 3 (2026-08-25 → 2026-11-02).
- Impact: The deliverable's *form* is decided by a hidden state machine, not by the work. Confidence fields are written under duress and are not comparable to normal ones.
- Fix: Clear the restriction on spawn; if it must persist, report it in the envelope so the orchestrator can discount the reply.
- Status: **LIVE**.

**AW-225 — Instance identity cannot be pinned; spawn-by-name minted a second instance**
- What: Attempting to address an instance by name to guarantee "same worker" produced two instances under one name; the workaround convention was "never use `default2`". That convention was later wrong (AW-222 requires stopping *both*), so the guidance inverted itself.
- Evidence: LL-10-29:L496 (2026-08-25).
- Recurrence: 1.
- Impact: The orchestrator has no primitive for "the same worker", so continuity is assumed, never established.
- Fix: Explicitly returned instance IDs on every reply and require them on the next call.
- Status: **SUPERSEDED** — the "never use default2" rule was replaced by the stop-both rule in AW-222.

**AW-226 — Spawn lockdown: worker born with `filesystem:banned`, every tool refused**
- What: On 2026-09-04 a spawned worker (`worker-91be4fa59d3f`) came up with `filesystem:banned` and could not execute anything: *"Tool requires filesystem:read, but session allows filesystem:banned."* Root cause: the effective permission set was mirrored from the **raw** `session_permissions` (`{}`) instead of `effective_permissions`. Three sites repeated the mistake in `tools/workspace/worker_thread.py` (LL-10-29:L716-751; resolved at L738).
- Evidence: LL-10-29:L716-751, LL-10-29:L738.
- Recurrence: 1 incident, 3 code sites.
- Impact: Blocks work: the worker is alive and answering but functionally shackled, and the error names a permission the operator never set.
- Fix: Mirror from `effective_permissions` at all three sites (done).
- Status: **FIXED**.

**AW-227 — The status vocabulary is undocumented and unvalidated**
- What: A source audit of the worker-harness status field (S3b) found the live vocabulary is `ready, busy, paused, completed, error, detaching, detached, killed` — with no validation tuple and 16 bare-literal assignment sites. The string the orchestrator actually observed, `stopped`, appears **only in a comment and is never assigned**, so it can only arise from a path nobody reviewed.
- Evidence: LL-11-05:L591 (2026-11-03, S3b audit).
- Recurrence: 1 audit; the ambiguity is permanent until modelled.
- Impact: **Causes wrong output** (see AW-222): the orchestrator branches on strings that no code guarantees, and one string in circulation is a ghost.
- Fix: One `Enum` (or frozen tuple) for status, validated on assignment and on read; delete comment-only states.
- Status: **LIVE**.

---

## THEME K — The two trees, the container, and the missing read primitive (AW-228 … AW-236)

Every worker operates on **two filesystems** at once — the host checkout and the container's `/workspace` — and the tool boundaries do not line up with that split. Worker tools are rooted at the host path and cannot see the container mount; container tools have no `git`; `/tmp` does not survive a tool call. The result is a cluster of traps that all produce *plausible output from the wrong tree*.

**AW-228 — Worker `FileEditor`/`ReadFile` are rooted at the host path and cannot reach `/workspace`**
- What: A worker cannot use `FileEditor` or `ReadFile` on container paths; edits must go through `DockerCodeRunner` with a heredoc Python script. On a 350-line file that consumed roughly 30% of a 150-second budget.
- Evidence: LL-11-05:L556 §1 (2026-11-03), LL-11-05:L475 §2.
- Recurrence: 3.
- Impact: Budget burn plus a class of transcript illegible to review (the edit exists only inside a heredoc).
- Fix: Mount the workspace at the host path the worker tools use, or give the worker a container-native editor.
- Status: **LIVE**.

**AW-229 — `FilePreviewTool` refuses absolute `/workspace` paths**
- What: The preview tool requires a path relative to its root and rejects `/workspace/...` outright, so the natural absolute path fails.
- Evidence: LL-11-05:L531.
- Recurrence: 2.
- Impact: Wasted calls; and because the *relative* form is accepted, a silently-different file can be previewed than the one named.
- Fix: Accept absolute paths, or state the root in the error message.
- Status: **LIVE**.

**AW-230 — `FileEditor` refuses worktree paths, so worktree edits are heredoc-only**
- What: Paths under `/workspace/.worktrees/<wt>/` are rejected by `FileEditor`, making the container heredoc the only way to edit a worktree.
- Evidence: LL-11-05:L383 (2026-11-02).
- Recurrence: 1 recorded; structurally constant for worktree work.
- Impact: Forces the highest-risk editing channel (AW-228) precisely where precision matters most.
- Fix: Allow worktree paths.
- Status: **LIVE**.

**AW-231 — "repo root" is ambiguous: `/workspace` is the MAIN repo, the worktree is `/workspace/.worktrees/<wt>`**
- What: Both are called "the repo". A tool given the wrong one returns the *other branch's* tree without complaint, so the worker reports that features from the branch under test are missing.
- Evidence: LL-11-05:L381, LL-11-05:L434-436 (2026-11-02).
- Recurrence: 3.
- Impact: Wrong output: "the feature isn't there" when the file read was from a different checkout.
- Fix: Tools should echo the resolved absolute root on every call; briefs should name the worktree, never "the repo".
- Status: **LIVE**.

**AW-232 — A prose warning in the brief is not an anchor, and the anchor inverted**
- What: A brief that *warned in prose* about the two trees did not stop a worker from editing `/workspace/docs/...` (the main checkout) while treating the worktree copy as the "pristine original" — i.e. it inverted which tree was authoritative.
- Evidence: LL-11-05:L257-262 (2026-11-01).
- Recurrence: 2.
- Impact: Wrong-tree edits with an internally consistent (and therefore convincing) account.
- Fix: Make "print the absolute target path" the brief's first required action, or move the work to a worktree via `stash_push`/`stash_pop` so only one tree exists.
- Status: **LIVE**.

**AW-233 — Live incident: worktree facts, main-checkout edit, false "does not exist" reports**
- What: A worker given worktree facts edited the main checkout, then reported *"`make_system_notice()` does not exist"* and *"[System Notice: prefix absent"* — both true of the tree it edited and false of the tree it was asked about. Recovery required `md5` comparison because the container has no `git`.
- Evidence: LL-11-05:L394 (2026-11-02).
- Recurrence: 1 (incident), same root as AW-231/232.
- Impact: Blocks work; the two false negatives nearly triggered a re-implementation of code that already existed.
- Fix: Assert one branch-only fact (a symbol that exists only on the target branch) *before* any edit.
- Status: **LIVE**.

**AW-234 — `/tmp` does not persist between tool calls, so split mutate/revert probes silently no-op**
- What: A probe designed as copy → mutate → run → revert across separate tool calls finds nothing to mutate (or nothing to revert) because `/tmp` is per-call. The probe then reports "the test does not bite" — a **false** negative produced by the harness, not by the code.
- Evidence: LL-11-05:L384, LL-10-29:L1296 (2026-09-21), LL-10-29:L1541 (2026-09-23).
- Recurrence: 3 (2026-09-21 → 2026-11-02).
- Impact: **Wrong output**, and of the worst kind: it certifies a weak test as adequate.
- Fix: One tool call must contain the whole probe; or use a persisted path under `/workspace`.
- Status: **LIVE**.

**AW-235 — No `git` binary in the container** — *by design*
- What: `git` is absent (`/bin/sh: 1: git: not found`). Agents must prove state by checksum (`md5sum`) and must run `git status` / `git diff` themselves from the orchestrator side.
- Evidence: LL-11-05:L430-431, LL-11-05:L500 §8, LL-11-05:L582 §8, LL-10-29:L1205 (2026-09-20).
- Recurrence: 4.
- Impact: Every verification becomes two steps across two tools; workers that forget report unverified claims.
- Fix: None wanted — this is deliberate isolation. The fix is documentation: briefs must say "no git in the container; checksum instead".
- Status: **LIVE (by design)**.

**AW-236 — vitest needs `TMPDIR` outside `/var/tmp`, and a symlinked `node_modules` defeats `.gitignore`**
- What: Two frontend traps. (a) Without `TMPDIR` set elsewhere, vitest dies with `ENOENT mkdir '/var/tmp/<rand>/web'` **before collection**, prints `Errors  1 error`, and still exits **0**. (b) `node_modules` is a symlink and `.gitignore`'s `node_modules/` pattern does not match a symlink, so `git status` shows `?? web_ui/frontend/node_modules`.
- Evidence: LL-11-05:L200-201 (2026-10-31, "Frontend test + worktree traps (Gen 55, W2 landing-card fix)"), LL-11-05:L392, LL-11-05:L203.
- Recurrence: 3.
- Impact: **Wrong output** twice over: a green exit code from a suite that never ran, and a dirty tree that looks like a stray artifact.
- Fix: Always export `TMPDIR=$PWD/.tmp`; ignore `node_modules` (no trailing slash) or unlink the symlink.
- Status: **LIVE**.


---

## THEME L — The git surface: parameters that are accepted and then ignored (AW-237 … AW-249)

This is the costliest theme in the archive, and its shape is uniform: a parameter is *accepted*, *not applied*, and *not rejected*. No exception is raised. The caller gets a well-formed answer to a different question. The orchestrator spends a substantial fraction of its budget on `git_read` and cannot tell a real answer from a truncated or empty one.

**AW-237 — `git_read show_file` ignores `line_start`/`line_end`, dumps the file, and hard-truncates at 10 000 tokens**
- What: Range parameters are inert on `show_file`. The tool returns the file from line 1 and cuts off at a token ceiling, so a 901-line document is unreadable past roughly line 330 — with no reliable indication that the tail exists. This is the **most-cited single defect in the archive**, and it was independently re-verified in a later generation ("TOOL LIMITS (Gen 56, verified twice)") and re-stated a third time as "TOOL REALITY (bank it)".
- Evidence: LL-11-05:L401 (*"### git_read: `line_start`/`line_end` are inert on `show_file`"*), LL-11-05:L490, LL-11-05:L275, LL-11-05:L251; BF:L693, BF:L768, BF:L847; LL-10-29:L1391 (reading `server.py` blew 50k tokens).
- Recurrence: 7+ (2026-09-22 → 2026-11-03).
- Impact: Costs thousands of tokens per read and, worse, produces **false negatives** — the code you needed was past the cut.
- Fix: **Already validated, never applied.** `git_read operation="blame"` with `file_path` plus `line_start`/`line_end` *does* honour the range (recorded three times: BF:L693, BF:L768, BF:L847). Wire that path into `show_file`.
- Status: **LIVE**.

**AW-238 — `ls_tree` ignores `prefix` and returns a truncated whole-repo tree**
- What: `prefix` is inert; the tool returns the entire tree, truncated — and it truncated *before* `tools/`, so the orchestrator inferred that a directory did not exist from a dump that simply had not reached it.
- Evidence: LL-11-05:L408 (2026-11-02), LL-11-05:L510 (2026-11-03 — ~10k tokens spent at a 77k/85k budget).
- Recurrence: 3.
- Impact: False absence; ~10k tokens per call.
- Fix: Honour `prefix`, or reject it. Cheaper workaround: list/grep the worktree through a worker (~20 s, ~200 tokens).
- Status: **LIVE**.

**AW-239 — `log` ignores `a`/`b` and `file_path`, returning a plausible wrong history or nothing at all**
- What: `a`/`b` are ignored (asked for `a=dev b=HEAD`, got ~50 commits of `dev`'s own history). `file_path` on `log` returns **EMPTY** — indistinguishable from "this file never changed". In the recorded case the orchestrator nearly concluded a design document had never been committed.
- Evidence: LL-11-05:L442, LL-11-05:L457 §2 (*"a FALSE NEGATIVE, which is worse than a missing feature"*), LL-11-05:L488; LL-10-29:L1121-1142 (2026-09-19/20).
- Recurrence: 4.
- Impact: Wrong output; historical claims reversed on the basis of an empty result.
- Fix: Reject unsupported parameters loudly rather than ignoring them.
- Status: **LIVE**.

**AW-240 — `show`/`diff` with `commit1`+`commit2`+`file_path` return silently empty**
- What: Same class as AW-237/239, stated as a synthesis: *"Every range/selector parameter I have tried on `git_read` (`line_start`, `line_end`, `prefix`, `a`, `b`) has been silently ignored while `format`, `max_count`, `file_path` work … the failure mode is **wrong answers**, not errors."*
- Evidence: LL-11-05:L321 (2026-11-02), LL-11-05:L446-448.
- Recurrence: 2 (as a synthesis; the underlying cases are counted above).
- Impact: The orchestrator cannot distinguish "no difference" from "parameter dropped".
- Fix: Validate the parameter matrix per operation; error on unsupported combinations.
- Status: **LIVE**.

**AW-241 — The truncation marker is written *inside* the content, so the tail reads as the end of the file**
- What: `"… (output truncated to 10000 tokens, original was 10025 tokens)"` is appended into the content body rather than emitted as tool metadata. A reader skimming the tail has no structural signal, and concludes the file ends there — hence "does not exist".
- Evidence: LL-11-05:L414-418 (*"### Silent truncation at 10 000 tokens"*).
- Recurrence: 1 recorded; structurally present on every truncated read.
- Impact: Amplifier for AW-237/238/239 — converts a cost problem into a correctness problem.
- Fix: Emit truncation as structured tool output (a flag + counts), never as in-band text.
- Status: **LIVE**.

**AW-242 — No `commit --amend` and no message rewrite; the message is only visible *after* it is immutable history**
- What: There is no amend and no way to correct a commit message. The echo of the message is returned after the commit exists, so a wrong claim in it ("147 tests green", when the true count was 149) is permanent.
- Evidence: LL-11-05:L520 (2026-11-03).
- Recurrence: 1 recorded; the capability gap is permanent.
- Impact: Wrong output that **cannot be corrected** — history now asserts something false.
- Fix: Expose `amend` gated on "not yet pushed", or render the message for confirmation *before* writing it.
- Status: **LIVE**.

**AW-243 — `worktree_add` has five distinct traps, and the failures are diagnosable only by trial**
- What: (1) The undocumented `path` argument is validated against the **host** root while `working_dir` takes the **container** path — an absolute `/workspace/...` is rejected as "outside workspace"; it must be relative `.worktrees/x`. (2) `base` names what gets **checked out**, so `worktree_add(base="dev")` fails whenever `dev` is checked out anywhere — the correct sequence is `branch_create(base=dev)` then `worktree_add(base=<new branch>)`. (3) `"'dev' is already used by worktree"`. (4) A worktree can come up **DETACHED** even with a branch named, and then every commit dies with *"refusing to commit: the workspace HEAD is detached"*. (5) It reports `(base: HEAD)` even when a branch was named.
- Evidence: LL-11-05:L459 §3 (cost 2 failed calls), LL-11-05:L498 §7, LL-11-05:L202, LL-11-05:L208, LL-11-05:L511 §2.
- Recurrence: 5 (2026-10-31 → 2026-11-03).
- Impact: Blocks work; each trap is discovered by burning a call, and (4) is invisible until the first commit attempt.
- Fix: Accept absolute container paths; make `base` mean "branch to create from"; verify and report HEAD's branch after creation; never report `HEAD` when a branch was named.
- Status: **LIVE**.

**AW-244 — `git_write` is refused at the critical token threshold**
- What: When context reaches the critical level the session is restricted to `Respond` + `SummarizeTool`, and **`git_write` is gated too** — *"the one irreversible action (commit) is gated on a condition the agent cannot fix by itself."* A summarise cycle is the only road back, and by then the plan may be gone.
- Evidence: LL-11-05:L476 §3, LL-11-05:L496 §6 (2026-11-03).
- Recurrence: 2.
- Impact: Blocks work at the worst possible moment — finished work cannot be landed.
- Fix: Commit before the threshold (operator rule); structurally, exempt `git_write` from the restriction or allow a single privileged commit.
- Status: **LIVE**.

**AW-245 — `diff` cannot see untracked files and offers no `--stat`/`numstat`**
- What: `diff` omits untracked files; `diff_cached` works only after staging; there is no stat/numstat mode, so answering "what am I about to commit?" requires dumping the full patch.
- Evidence: LL-11-05:L492 §4, LL-11-05:L461 §4.
- Recurrence: 2.
- Impact: Token cost on the exact verification step that should be cheap; untracked files are silently outside the diff's view entirely.
- Fix: Add `--stat`/`--numstat`, and include untracked files (`--no-index`-style or a status-aware listing).
- Status: **LIVE**.

**AW-246 — `stage` has no `-f`, so an ignored path forces a `.gitignore` edit into an unrelated commit**
- What: `git_write stage` cannot force-add an ignored path, so landing a 3-file docs commit required editing `.gitignore` as well.
- Evidence: LL-11-05:L494 §5.
- Recurrence: 1.
- Impact: Cosmetic commit pollution plus an integrity cost — a repo-wide ignore change rode along with a documentation commit.
- Fix: Add a force flag, or stage ignored paths explicitly.
- Status: **LIVE**.

**AW-247 — The orchestrator has no read/execute primitive at all**
- What: To read one 250-line *new* file, the orchestrator had to `stage` it and use `git_read diff_cached` as a reader, because untracked files are invisible to `diff` and `show_file`. Every read is therefore a git operation with a git operation's costs and side effects.
- Evidence: LL-11-05:L461 §4 (2026-11-03).
- Recurrence: 1 recorded as an incident; it is the **structural driver** of nearly every cost note in Themes L and M.
- Impact: Read-only inspection mutates the index. That is the root reason `stage`/`unstage` churn appears in sessions that never intended to commit.
- Fix: Bounded read-only `cat`/`find`/`grep` over the workspace (the fix explicitly requested in the source).
- Status: **LIVE**.

**AW-248 — `file_path`'s accepted shape differs per operation, and a directory pathspec commits everything under it**
- What: `git_write commit` rejects a list-valued `file_path` (it stringifies and the tool answers "pathspec wildcards not allowed") and ignores `paths=`. A **directory** pathspec, however, commits everything under it — one commit (`7cc00abf`) swept a whole directory that way. Meanwhile `git_read log` accepts `file_path=[...]` happily.
- Evidence: LL-11-05:L194 (2026-10-31), BF:L848.
- Recurrence: 2.
- Impact: Commit contents differ from what the caller enumerated; a "3-file commit" can become an arbitrary set.
- Fix: One shape (`list[str]`) everywhere, validated per operation, with directory pathspecs expanded and shown before writing.
- Status: **LIVE**.

**AW-249 — Total `git_read` outage: the resource-container policy read an in-memory mirror**
- What: Every `git_read` operation failed with *"containerized git execution unavailable: failed to create resource container: derived policy would widen: network_mode (failure_reason: container_create_failed)"*. The gate derives the container policy with `resolve_container_config(..., use_disk=True)`, but three RESOURCE-path callers omitted `use_disk`, so the policy came from an in-memory mirror that had lost its `network_mode`. At the previous HEAD the disk read failed closed, which meant git had been working **by accident**. The lesson recorded is the sharp one: *"a policy guard that ranks axes will silently ACCIDENTALLY ALLOW while the authoritative read is failing."*
- Evidence: BF:L543 (2026-09-27).
- Recurrence: 1 outage; the underlying "reads a mirror, not the authority" pattern is one occurrence.
- Impact: Blocks work — the entire git surface, read and write, was dead.
- Fix: Pass `session_id`, `workspace_id`, `use_disk=True` at `GitReadTool._resolve_resource_execution`, `provision_workspace_resource` and `_container_policy_denied`; regression test `tests/test_resource_container_disk_policy.py` with RED-before/GREEN-after plus a mutation counterfactual.
- Status: **FIXED**.

---

## THEME M — Verification discipline: green is not evidence (AW-250 … AW-258)

A distinct family of defects, none of them in the harness: the *evidence* the orchestrator accepts is structurally incapable of falsifying the claim it is used to support. Tests that cannot fail, sweeps that miss the file, logs that destroy the traceback, and prose that describes an architecture that was never built. These recur because each one is cheap and each one *feels* like verification.

**AW-250 — A test that survives the mutation is not a regression test**
- What: Reverting the fix left every existing test green. The two tests that looked like coverage were structurally incapable of reaching the defect. Related, from an earlier generation: *"removing a guard invalidates the tests asserting it."*
- Evidence: LL-11-05:L317 (2026-11-02), LL-10-29:L1683.
- Recurrence: 2.
- Impact: False confidence; a guard is deleted and its tests stay green, so the deletion is never noticed.
- Fix: Require RED under **two independent mutations** plus a byte-identical restore before a test counts as a regression test.
- Status: **LIVE**.

**AW-251 — A green suite is not evidence about a module it never imports**
- What: The suite can be green while the changed module is not imported by any test. The check is a grep of the test tree for the symbol; zero hits means the green suite says nothing about it.
- Evidence: LL-11-05:L371, LL-11-05:L225, LL-11-05:L478.
- Recurrence: 3.
- Impact: Wrong output at the meta level: "tests pass" reported for code no test touches.
- Fix: Grep the test tree for the symbol *before* quoting the suite as evidence.
- Status: **LIVE**.

**AW-252 — `-k` filtering is not a regression net**
- What: A P2 change broke `test_detach_cancels_an_in_flight_sleep`; the `-k` sweep passed because it never selected that test.
- Evidence: LL-11-05:L377 (2026-11-02).
- Recurrence: 1 recorded; the pattern is structural.
- Impact: A known break shipped on a green sweep.
- Fix: **Rule** — run the FULL suite once, to a log, before any merge.
- Status: **LIVE**.

**AW-253 — `| tail -n 40` structurally destroys pytest tracebacks**
- What: pytest prints FAILURES first and the summary last, so tailing the output keeps exactly the lines that contain no diagnostic. Cost: ~9 minutes of blind debugging in the recorded case.
- Evidence: LL-11-05:L477 (2026-11-03), LL-10-29:L1003 (2026-09-16).
- Recurrence: 3.
- Impact: Time sink; the failure is visible but its cause has been truncated away.
- Fix: `--tb=line`, or `tee` to a log and grep the log.
- Status: **LIVE**.

**AW-254 — `cmd | tail -25; echo EXIT=$?` reports `tail`'s status, not the producer's**
- What: `$?` is the last command in the pipeline. This is precisely how the vitest `TMPDIR` failure (AW-236) stayed green-looking: `Errors 1 error` in the body, exit 0 reported. `${PIPESTATUS[0]}` does not help — the shell is `dash`, and it answers `Bad substitution`.
- Evidence: LL-11-05:L201 (2026-10-31).
- Recurrence: 2.
- Impact: Wrong output: a failing gate reported as passing.
- Fix: Run without a pipe and capture `$?`, or `set -o pipefail` under bash.
- Status: **LIVE**.

**AW-255 — Scoped greens hide real breakage**
- What: The 49-failure class and the stale `_CANONICAL_PERMISSION_FIELDS` pin both lived in files outside every suite anyone had actually run.
- Evidence: LL-11-05:L341 (2026-11-02).
- Recurrence: 1 audit; the pattern is structural (see AW-252).
- Impact: The repo was broken in ways no green report covered.
- Fix: Coverage-by-file reporting: which test files exist and which were executed.
- Status: **LIVE**.

**AW-256 — Derived-vs-pinned invariants, and tickets that name the wrong guarantor**
- What: *"A set derived from the thing it checks can never fail, which silently deletes the test."* A test that computes its expectation from the implementation is not a test. Second half: a ticket naming the wrong guarantor is worse than no ticket — it directs the fix at a component that cannot enforce the property.
- Evidence: LL-11-05:L341, LL-11-05:L193 (2026-10-31).
- Recurrence: 2.
- Impact: A test that cannot fail, and a fix aimed at the wrong layer.
- Fix: Pin literals for invariants; name the enforcing symbol in every ticket.
- Status: **LIVE**.

**AW-257 — The worker substitutes a scoped version of the approved decision**
- What: Given "remove the field entirely", the worker shipped a 150-line scoped variant, self-reported honestly, silently skipped one of three requested artefacts and filed two that were not requested. Nothing was hidden — the substitution was in the *scope*, and the report read as agreement.
- Evidence: LL-11-05:L215 (2026-10-31).
- Recurrence: 1 recorded; recognisable pattern.
- Impact: The deliverable is not the approved design, and the diff has to be re-reviewed from scratch.
- Fix: Re-query with the approved shape **plus the argument taken from the worker's own report** (its stated reason for scoping is the thing to rebut).
- Status: **LIVE**.

**AW-258 — Documents that fabricate a refactor**
- What: Two of three documents claimed a frozen module "re-exports the shared gate". It did not. The prose described the intended architecture, not the repository.
- Evidence: LL-11-05:L219-223 (2026-10-31).
- Recurrence: 1 (two documents, one claim).
- Impact: Wrong output persisted into documentation, where it will be believed for longer than a chat message.
- Fix: Detector — grep prose for "re-export / imports / delegates / calls" and verify each claim against the source.
- Status: **LIVE**.

**Positive counter-example — the controls that do work.**
These belong in the archive because they are the same discipline pointed the other way, and because several distinguish this harness from a naive one:
- **Brief-audit before dispatch** falsified 3 of 5 items in 104 seconds (LL-11-05:L588).
- Auditing a brief pre-code caught 5 substantive errors and 1 hallucinated attribute (LL-11-05:L539-548), and then a *sixth*: *"Sixth hallucinated attribute in two briefs: `_runtime_sock`"* (LL-11-05:L560).
- **Workers correctly refuse a false premise** when the brief carries an explicit stop-gate — "do not change X, report instead" (LL-11-05:L535, L331, L352).
The lesson is that the audit step, not the model, is what catches hallucinated attributes — and that a stop-gate converts a worker from an eager patcher into a reporter.


---

## THEME N — Knowledge-base artefacts and harness primitives (AW-260 … AW-268)

The archive itself is an artefact produced by these tools, so the same traps show up *in the record of the traps*: headings that cannot be counted, paths that no longer exist, heredocs that preserve backslashes, and "rollback" helpers that do not roll back. Recorded here so the next archivist does not repeat them.

**AW-260 — KB rot is invisible until you count**
- What: `INFRA_ONBOARDING.md` named ~31 files; 19 of them no longer existed. Nothing had flagged the drift, because nothing ever checked.
- Evidence: LL-11-05:L504 (2026-11-03).
- Recurrence: 1 audit; unbounded by default.
- Impact: An onboarding document that describes a repository which no longer exists — and every agent that reads it inherits the phantom paths.
- Fix: A check that greps every backticked KB path and reports the misses.
- Status: **LIVE**.

**AW-261 — Doubled heading prefix: `## 2026-10-29 — ## Real Title`**
- What: 37 lesson headings and 24 tracker headings were written with the date prefix *and* the original `##` retained. Any count of `^## ` headings overcounts by ~61.
- Evidence: LL-11-05:L151.
- Recurrence: 1 (systemic, ~61 headings).
- Impact: Structural counts and heading-based harvesting are both wrong; this report's own per-file heading counts carry the caveat.
- Fix: Strip the inner `##` on ingest; validate headings with a schema.
- Status: **LIVE**.

**AW-262 — Quoted-heredoc payloads preserve backslashes literally**
- What: Inside a quoted heredoc nothing is interpolated, so an escape emitted as `\`` lands as backslash-plus-backtick in the file — the markdown renders a stray backslash, and a grep for the construct then fails to match what you thought you wrote.
- Evidence: LL-11-05:L179 (2026-10-29).
- Recurrence: 1 recorded; latent on every heredoc write.
- Impact: Subtly corrupted documents and grep-based verification that cannot confirm the write.
- Fix: Emit bare backticks, or grep for `\\` immediately after the write.
- Status: **LIVE**.

**AW-263 — `_turn_cancel` is cleared at turn *start*, so a cancel between turns is discarded**
- What: A cancel issued while no turn is running is cleared when the next turn begins. There is no error and no acknowledgement — the cancel simply never happens.
- Evidence: LL-11-05:L592 (2026-11-03).
- Recurrence: 1 audit.
- Impact: Makes "cancel anything, anytime" false. Work the operator believes is stopped continues to completion.
- Fix: Do not clear a pending cancel at turn start; either honour it on arrival or ack the refusal.
- Status: **LIVE**.

**AW-264 — A blanket `except Exception: pass` manufactured lying success**
- What: A `try/except: pass` wrapped around `registry.register` returned `{job_id, status: "submitted"}` for a job that no `job_status` path could ever resolve. The pattern had been copy-pasted twice.
- Evidence: LL-11-05:L307 (2026-11-01).
- Recurrence: 1 incident, 2 copies.
- Impact: A fabricated job id — the caller polls a job that does not exist and never learns why.
- Fix: Narrow the except; re-raise or report the failure in the response.
- Status: **FIXED**.

**AW-265 — A "rollback" helper may not touch the thing you think**
- What: `_discard_pending_job` removed the id from the bookkeeping dict only; the queued tuple still sat in `_input_queue`, so the job ran anyway. The rollback rolled back nothing.
- Evidence: LL-11-05:L309.
- Recurrence: 1.
- Impact: Cancelled work executes; the queue and the index disagree permanently.
- Fix: `withdraw_queued_query`, which removes from the queue itself (done).
- Status: **FIXED**.

**AW-266 — Insertion-by-line-number specs need the blank-line convention stated**
- What: A brief specifying "insert at index 3" is ambiguous about whether the separating blank line is inside or outside the insertion. Unstated, the worker's choice is arbitrary and the spec is untestable.
- Evidence: LL-11-05:L502.
- Recurrence: 1.
- Impact: Cosmetic diffs and re-review; occasionally a broken test fixture.
- Fix: State the convention explicitly ("index 3, exactly one blank line separates").
- Status: **LIVE**.

**AW-267 — A double-selector RED in `GitReadTool._git_show_file`, hidden by an untracked test file**
- What: The test file is untracked, so `git_read diff tools/git_info_tool.py` returns **EMPTY** and only `git status --porcelain` reveals that anything changed. Reproducing the RED condition therefore requires a selector the reader cannot discover.
- Evidence: BF:L391.
- Recurrence: 1; open.
- Impact: Investigating this bug with `git_read` reproduces AW-245 and AW-239 at once.
- Fix: Stage the test file before diffing, or use `git status --porcelain` as the discovery step.
- Status: **OPEN**.

**AW-268 — Detached-HEAD fail-closed gates added for `commit`, `branch_create`, `worktree_add`**
- What: `_is_detached_head` was added and gates three write sites, so a detached worktree (AW-243 trap 4) now fails closed with a named reason instead of committing onto a detached HEAD.
- Evidence: BF:L381.
- Recurrence: 1.
- Impact: Positive: it converts AW-243's worst trap into a diagnosable error.
- Fix: n/a — already gated at 3 sites.
- Status: **FIXED**.

**Empty result, stated explicitly.** Five knowledge-base files were swept and contain **no** worker-harness defects: `task_tracker.md`, `task_tracker_archive_2026-10-29.md`, `open_items_register.md` (which carries an explicit `SUPERSEDED 2026-10-21 Gen 47` banner — its contents are stale by declaration), `working_agreements.md`, and `status.md`. `working_agreements.md` does contain operating rules of the kind Theme M recommends; they are rules, not defects, and are not counted as items.

---

## 3. FAMILIES

The 65 items are not 65 independent bugs. They collapse into nine families, each defined by a single structural property of the harness. Fixing the family is worth more than fixing the item.

**(1) UNDELIVERED ENVELOPE** — the reply envelope is dropped or corrupt, so the orchestrator reasons about a result it did not receive.
Members: AW-201, AW-202, AW-203, AW-204, AW-205, AW-206, AW-207 (plus one LL-10-29 case, 2026-09-20). Span: 2026-09-20 → 2026-11-03 — and still **BROKEN** per `project/vision_ledger.md:24`.

**(2) VARIABLE / HIDDEN CAP** — a limit exists, is not documented, and varies by path, so effort is silently truncated.
Members: AW-210 … AW-219, AW-224. Span: 2026-08-07 → 2026-11-03.

**(3) INSTANCE-REUSE-POISON** — a spawn reuses or inherits state, so the worker is not the worker you think you have.
Members: AW-220, AW-221, AW-222, AW-223, AW-225, AW-226. Span: 2026-08-24 → 2026-11-03.

**(4) TWO-TREES-ONE-PATH** — the host checkout and the container mount are both "the repo"; tools disagree about which one they mean.
Members: AW-228, AW-229, AW-230, AW-231, AW-232, AW-233, AW-235. Span: 2026-11-01 → 2026-11-03.

**(5) ACCEPTED-AND-IGNORED** — a parameter is validated as present, then not applied, and not rejected. The caller receives a well-formed answer to a different question.
Members: AW-237, AW-238, AW-239, AW-240, AW-248. Span: 2026-09-19 → 2026-11-03.

**(6) FALSE-NEGATIVE-AS-ANSWER** — an empty or truncated read is read as a true negative ("it does not exist", "it never changed").
Members: AW-237, AW-239, AW-240, AW-241, AW-250, AW-255. Span: 2026-09-22 → 2026-11-03.

**(7) GREEN-IS-NOT-EVIDENCE** — passing output is accepted as proof of a property the test or log structurally cannot observe.
Members: AW-250, AW-251, AW-252, AW-253, AW-254, AW-255, AW-256, AW-258. Span: 2026-09-16 → 2026-11-02.

**(8) ORCHESTRATOR-BLIND-READ** — the orchestrator has no read primitive; every inspection is a git write-shaped operation with git-sized costs and side effects.
Members: AW-228, AW-229, AW-230, AW-234, AW-242, AW-244, AW-245, AW-247. Span: 2026-11-02 → 2026-11-03.

**(9) AUTO-SPEC-DRIFT** — briefs, docs and tickets describe the intended architecture rather than the built one, and are believed.
Members: AW-207, AW-225, AW-258, AW-261, AW-266, AW-268. Span: 2026-08-25 → 2026-11-03.

---

## 4. STATUS SUMMARY

| Status | Count | Items |
|---|---|---|
| **STILL LIVE** | 58 | AW-201 … AW-207, AW-210 … AW-219, AW-220 … AW-224, AW-227, AW-228 … AW-236, AW-237 … AW-248, AW-250 … AW-258, AW-260 … AW-263, AW-266 |
| **FIXED** | 5 | AW-226, AW-249, AW-264, AW-265, AW-268 |
| **SUPERSEDED** | 1 | AW-225 |
| **OPEN** (under investigation) | 1 | AW-267 |
| **TOTAL** | **65** | |

Two observations the table makes plain:
- All five fixes that exist sit in *harness code* (permission mirror, container policy, register/rollback, detached-HEAD gates). Not one of them is in the git read surface or the envelope — the two families the ledger still records as **BROKEN**.
- AW-237, the single most-cited defect in the archive, has a fix that was validated in the field three separate times and never landed. It is a one-line wiring change against a path (`blame`) that already honours the parameters.

---

## 5. LIMITATIONS OF THIS REPORT

Stated plainly, because the counts above should be read with these caveats:

- **Two sittings.** Themes H and I were written before a token gate restricted tool access; Themes J, K, L, M and N were written afterwards, from notes rather than fresh source reads. Item *format* is uniform; item *depth* is not perfectly so.
- **Coverage per file is approximate.** See the table in §1. Percentages there are judgement estimates from heading sweeps, not exhaustive line-by-line reads.
- **Recurrence counts are lower bounds.** The failure mode hides its own occurrences: a truncated query looks like a short query, a dropped envelope looks like a terse worker, and an empty `log` result looks like a file that never changed. Every "rec 3" should be read as "≥3".
- **`bugs_and_fixes_archive_2026-11-05.md` yielded no tooling-specific headings.** It was swept and contributed nothing directly; `bugs_and_fixes.md` (the live file) did.
- **`agent_worker_timeline.md` was NOT harvested.** Its headings do not match `^## `, so a heading-based pass returns nothing. It is 776 lines and substantially overlaps the lessons archives — a targeted (not heading-based) pass would be needed, and that work is not done here.
- **`handoffs.md` was only head-swept plus heading-scanned**, not read exhaustively.
- **Line numbers.** Where a source is the 2026-10-29 archive, the cited line is the archive line; the convention throughout is that the original line equals the archive line minus 4. Citations mixing both conventions are marked with their file label (`LL-10-29:` vs `LL-11-05:`).
- **AW-267 is the one item marked OPEN rather than LIVE or FIXED** — an investigation is in progress on it and it should not be read as resolved.

