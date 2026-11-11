# Agent / Worker Problems — Consolidated

Editorial consolidation of `PART1` (81 items, AW-001…081), `PART2` (76, AW-101…176) and `PART3` (65, AW-201…268) — **222 raw items → 49 problems** in 9 themes. Nothing new was measured for this document; every number below was already recorded in the parts.

## 0. BOTTOM LINE

Five root causes carry almost everything in this record.

1. **The brief is treated as a transport detail, not as the product.** Keys are dropped, long queries are truncated and then retranscribed by the receiving agent, and the brief is thrown away at summarisation (#1, #2, #4).
2. **No clock is declared, so every clock is guessed.** Ten measured caps from 150 s to 960 s, none of them the value that was asked for, and finished work is discarded at the wall (#5, #6, #7).
3. **Nothing owns the envelope.** ~17 writers set `status`/`confidence`/`timeout_triggered` under no schema, and six egress families deliver six different event shapes (#14, #16).
4. **Two trees are live at once and no surface says which one it touched** — host root vs `/workspace`, read-only container vs `git_*` tools (#27, #28, #29).
5. **Verification is narrated, not performed.** Tests that cannot fail, HEADs quoted from memory, and accepted parameters that are silently ignored (#22, #20, #32).

**Fix clause.** One declared clock, one envelope schema, one canonical path per file, one read primitive, and evidence-or-nothing on every claim before it reaches a report.

**What engineers gain.** Dispatch that delivers what was written; a timeout number that means what it says and returns the payload it already paid for; a status field that can be trusted in a dashboard; and a test result that can be believed — the four things that currently make every downstream fix unverifiable.

## 1. HOW TO READ THIS

- §2 is the census, ordered by theme; numbers #1…#49 are stable and are the ones to cite.
- Every problem carries `Evidence` (which part, which raw items), `Recurrence` (summed raw count and how far it spread), `Priority`, and `Members`.
- **Recurrence is a floor, not a census** — see §6. Treat `1` as "reported once, plausibly still true".
- Impact is one of four classes: *blocks work*, *costs time*, *produces wrong output SILENTLY*, *cosmetic*.
- §3 is the same 49 as a table with sequencing; §4 lists what is already fixed (read it before filing a bug); §5 is the uncomfortable part.
- Items already fixed stay in their theme's `Members` list, marked `(fixed)`, because their residual is usually the interesting half.

## 2. PROBLEMS BY THEME

### T1 — Dispatch: what the worker is actually given

Four problems, one subject: the hand-off. Between the orchestrator's intent and the worker's first tool call there are at least four transformations (field projection, truncation, re-serialisation, summarisation) and every one of them can lose content without an error.

### 1. Deliver the whole brief — every key, unaltered, or refuse

**What happens.** Dispatch projects the task onto a subset of keys, so a path or constraint that lives only in `briefing` or `meta` never reaches the worker; long queries are truncated and then *retranscribed* by the receiving side, so what arrives is a paraphrase of the tail rather than the tail. In the worst recorded case a five-path brief carried its fifth path only in `briefing` and the worker executed a four-item task and reported success; a 120-line brief arrived so degraded that the produced document was "entirely about Docker container registries".
**Evidence.** `part1 AW-001, AW-002, AW-003; part2 AW-101, AW-102, AW-104, AW-105; part3 AW-201, AW-202, AW-203`
**Recurrence.** 10 items, all three parts — structural (every dispatch pays it), not situational.
**Impact.** Blocks work — the worker cannot do what it was never told, and does not know it wasn't told.
**Action.** Stop projecting: pass the request payload through verbatim; if a size cap bites, fail the dispatch with the cap and the refused length rather than silently shortening; never retranscribe a truncated tail.
**Priority.** ACT NOW
**Members.** AW-001, 002, 003, 101, 102, 104, 105, 201, 202, 203

### 2. Keep the brief alive for the whole unit of work

**What happens.** The brief is a message, so summarisation is free to summarise it. A 15-item recon brief was compacted mid-run and items 1–8 vanished from the worker's view while the worker kept working; a 21-item / 6 KB replacement payload was lost the same way, twice.
**Evidence.** `part1 AW-008, AW-020; part2 AW-103; part3 AW-206`
**Recurrence.** 4 items across all three parts, 11 downstream sightings — every long unit of work eventually hits a summarisation boundary.
**Impact.** Blocks work — the worker silently executes the surviving subset of its instructions.
**Action.** Pin the original brief as a non-summarisable artefact with its own id, re-injected verbatim after every compaction; summarisation may summarise *progress*, never *instructions*.
**Priority.** NEXT
**Members.** AW-008, 020, 103, 206

### 3. Make the documented calling convention the real one

**What happens.** The tool schema says `worker_name` is optional and the runtime rejects the call with `worker_name is required for action 'spawn'`; the same class of drift makes `{"arguments": {}}` fail and leaves `stop` out of the docstring entirely, so an agent that follows the documentation cannot even end a worker it started.
**Evidence.** `part1 AW-005, AW-006; part2 AW-165; part3 AW-204, AW-205`
**Recurrence.** 5 items, three parts; every new agent rediscovers it once (≈15 wasted calls in the record).
**Impact.** Costs time — a discoverable-by-trial only contract.
**Action.** Generate the docstring from the schema and the validator from the same source; add a contract test that calls every documented action with only documented fields.
**Priority.** NEXT
**Members.** AW-005, 006, 165, 204, 205

### 4. Verify the brief's premises before dispatch

**What happens.** Briefs are written from memory of the repo and dispatched without a check: one named a `personal/parking_lot.md` that did not exist, one handoff specified a filename that was never created, and a spot audit of a single engineer-written brief found **5 substantive errors plus one hallucinated attribute** about a file that did exist.
**Evidence.** `part1 AW-009; part2 AW-110, AW-174; part3 AW-207, AW-266`
**Recurrence.** 5 items, three parts + 6 errors surfaced by one audit — the audit ratio suggests the rate is high, not rare.
**Impact.** Costs time — the worker spends its budget reconciling a brief with reality, then reports the reconciliation as the task.
**Action.** Cheap pre-flight: resolve every path/ref named in a brief and refuse the dispatch on a miss, quoting the missing path; keep the audit as a required step for briefs above some size.
**Priority.** NEXT
**Members.** AW-009, 110, 174, 207, 266

### T2 — Wall clock: budgets, kills, and what happens at the boundary

Six problems. The invariant the whole record keeps reaching for is simple — *a declared budget is honoured, and a payload that finished is returned* — and it is violated at every layer: queue, worker, turn, container, tool.

### 5. Declare one clock, and make every layer read that one

**What happens.** Caps are scattered and undeclared, so the number an agent passes is decorative: `timeout_seconds=900` and `timeout_seconds=150` produce the same kill, a request for 145 s ran to 470 s, and measured kills landed at 150, 165–200, 200, 350, 420, 470, 600, 628, 660 and 960 s depending on which layer fired first. `DockerCodeRunner` returned `exit_code: -2, timed_out: true, duration: 420.455s` with empty stdout against a limit it never disclosed.
**Evidence.** `part1 AW-011, AW-014; part2 AW-111, AW-114, AW-118; part3 AW-210, AW-211, AW-212, AW-213` (+ fixed: `part2 AW-112, AW-113`)
**Recurrence.** 11 items, all three parts, 40 downstream sightings — the single most-recurring problem in the record.
**Impact.** Produces wrong output SILENTLY — every reported duration is a different clock's opinion.
**Action.** One `TimeBudget` object resolved once at dispatch, propagated to queue/worker/turn/container, surfaced in every envelope; the effective value (and which layer clamped it) logged on every kill; a requested budget above the ceiling fails at dispatch, not at 470 s.
**Priority.** ACT NOW
**Members.** AW-011, 014, 111, 112 (fixed), 113 (fixed), 114, 118, 210, 211, 212, 213

### 6. Never discard a payload that finished

**What happens.** Work completes and is then killed before the response is serialised: one worker finished nine verification steps and was killed before `Respond`, and a follow-up query returned the whole result in 7 s — the work existed, only the delivery was lost. The same failure reads as "excellent research and no files".
**Evidence.** `part1 AW-012; part2 AW-162; part3 AW-216`
**Recurrence.** 3 items, three parts, 7 sightings — always at the boundary, always total loss.
**Impact.** Blocks work — full budget spent, zero artefacts, no way to tell it from a worker that did nothing.
**Action.** Persist the payload as soon as it exists (before any post-processing), then attach it to the terminal envelope whatever killed the turn; make "killed with payload present" a deliverable outcome, not an error.
**Priority.** ACT NOW
**Members.** AW-012, 162, 216

### 7. Make stop, kill and pause mean something

**What happens.** There is no cancellation path that reaches a running thread: the 600 s `SPAWN_QUEUE_TIMEOUT` is a queue wait, not a kill; an agent cannot `stop` a worker while it is itself blocked; and the work-loss-prevention machinery that would make pause safe ("WLM") ships off by default.
**Evidence.** `part2 AW-115, AW-116, AW-117, AW-119, AW-120, AW-167; part3 AW-263, AW-264 (fixed), AW-265 (fixed)`
**Recurrence.** 10 items, 20 sightings; every operator who tried to stop something.
**Impact.** Blocks work — runaway workers can only be waited out.
**Action.** A real cancellation token checked at every tool boundary and between turns, plus an operator-side kill that does not depend on the caller's own loop being free; ship WLM on.
**Priority.** NEXT
**Members.** AW-115, 116, 117, 119, 120, 121 (fixed), 167, 263, 264 (fixed), 265 (fixed)

### 8. Stop promising background work you do not perform

**What happens.** The surface offers background execution and then runs it in the foreground: 6,042 tests were dispatched as ~8 sequential foreground chunk calls, and a 400 s background run was lost with its log stopping at 64 % — the "background" job had no separate lifetime to survive in.
**Evidence.** `part1 AW-017; part3 AW-217, AW-218`
**Recurrence.** 3 items, two parts, 9 sightings.
**Impact.** Costs time — long jobs are chunked by hand and any chunk failure loses the tail.
**Action.** Either implement a real background job (own id, own lifetime, pollable status) or remove the word from the surface and document the chunking pattern with its budget arithmetic.
**Priority.** NEXT
**Members.** AW-017, 217, 218

### 9. Checkpoint deterministically, and make pause/resume stick

**What happens.** Resumption depends on narrative state rather than recorded state: a worker stopped mid-turn resumes from what it can reconstruct from its own summary, so pause/resume either replays a step or skips one, and the "where am I" answer differs between the paused and resumed instance.
**Evidence.** `part2 AW-122, AW-123, AW-124, AW-133, AW-141, AW-143` (+ fixed: `AW-132`)
**Recurrence.** 6 items + 14 sightings; only visible on multi-turn units (which is exactly where it costs most).
**Impact.** Produces wrong output SILENTLY — a resumed unit can silently re-execute or omit a step.
**Action.** Explicit checkpoint records (step id, next action, artefact ids) written at tool boundaries; resume reads the record, never the prose.
**Priority.** NEXT
**Members.** AW-122, 123, 124, 132 (fixed), 133, 141, 143

### 10. Orchestrator turn limits must not force a mid-unit handoff

**What happens.** When the orchestrator's turn budget expires the unit is handed off mid-step, so the next orchestrator starts by re-deriving what the previous one was doing from a partial transcript.
**Evidence.** `part2 AW-163`
**Recurrence.** 1 item, 3 sightings — the mechanism is general even if the report is single.
**Impact.** Costs time — a re-derivation tax on every long unit.
**Action.** Let the budget boundary land between steps, with the checkpoint record (#9) as the handoff payload.
**Priority.** MONITOR
**Members.** AW-163

### T3 — Ceilings: revocation, numbers, and the mechanism behind both

Three problems. The token ceiling is the one limit every agent meets by definition, and it is enforced at the worst possible moment with numbers that do not agree with each other.

### 11. Revocation must not land in the middle of an edit

**What happens.** The ceiling is crossed *inside* a tool call, so the call is refused and the context is then restricted: `ApplyEdits` came back refused with reason `token` mid-edit, at ~80–85k in one run and ~89k in another where the tools were simply gone; the restriction then survived one spawn, so the fresh worker inherited it.
**Evidence.** `part1 AW-018, AW-021; part2 AW-131; part3 AW-219, AW-224`
**Recurrence.** 5 items, all three parts, 15 sightings.
**Impact.** Blocks work — writes are the thing being done when the ceiling hits, so the loss is always an uncommitted edit.
**Action.** Revoke at a turn boundary, never inside a pending tool call; finish and report the in-flight write first; make the restriction instance-scoped and clearable (#45, #46).
**Priority.** ACT NOW
**Members.** AW-018, 021, 131, 219, 224

### 12. Make the ceiling a number, not folklore

**What happens.** Three different effective values coexist — a 85,000 default, a 80,000 `default2`, and a complete report delivered at 84,635 — while the warning fires at "67k" for a 65,000 context, i.e. the warning thresholds and the enforcement threshold are separate constants nobody reconciles. The WARNING tier does not restrict anything, so an agent can be well past the advertised limit and still going.
**Evidence.** `part1 AW-019; part2 AW-128, AW-129, AW-130, AW-134`
**Recurrence.** 5 items, two parts, 11 sightings.
**Impact.** Produces wrong output SILENTLY — every agent reasons about a budget that is not the one enforced.
**Action.** One constant, exposed in the tool response (remaining budget), with warning at a stated fraction of *that* number and enforcement only at turn boundaries.
**Priority.** NEXT
**Members.** AW-019, 128, 129, 130, 134

### 13. Prove the mechanism before shipping the fix

**What happens.** A token-accounting fix (F1–F4) was shipped on the strength of a "68k → 136k doubling" theory; instrumented spawns afterwards measured growth of +1,987 / +2,752 / +454 tokens — three orders of magnitude away from the theory, i.e. the diagnosis that justified the fix was never measured.
**Evidence.** `part2 AW-127`
**Recurrence.** 1 item, 4 sightings — a process failure rather than a defect, but it is the reason #12 keeps reappearing.
**Impact.** Costs time — fixes land without a measurable before/after.
**Action.** For anything with a number in the claim, attach the instrumented reading before and after; a fix without both is a hypothesis.
**Priority.** MONITOR
**Members.** AW-127

### T4 — Envelope truth: what the system says it did

Six problems. Every consumer — the orchestrator, the panel, the hooks, the human reading the transcript — reads a different subset of the same under-specified response, so "what happened" has no single answer.

### 14. One meaning for `status`, `confidence` and `timeout_triggered`

**What happens.** Roughly 17 writers set these fields with no shared schema: `status: null` is returned when a query is stopped mid-flight; `timeout_triggered: true` is returned alongside complete content; the same field reports `false` for a run that took 245.7 s against a 240 s cap. `stopped` exists only as a comment in the writer set — no writer produces it.
**Evidence.** `part1 AW-013, AW-015; part2 AW-137, AW-138, AW-139, AW-140, AW-142; part3 AW-215, AW-227`
**Recurrence.** 9 items, all three parts, 30 sightings — the per-field cost is paid on every single call.
**Impact.** Produces wrong output SILENTLY — an envelope cannot be used to decide whether to retry.
**Action.** One enum per field declared in one module, one constructor for the envelope, no writer allowed to build the dict by hand; `timeout_triggered` derived from the clock that actually killed it (#5).
**Priority.** ACT NOW
**Members.** AW-013, 015, 137, 138, 139, 140, 142, 215, 227

### 15. Never answer the previous query

**What happens.** A query that takes 133.6 s is followed by a query answered in 29.8 s — with the *first* query's answer, because the response was matched to the wrong request.
**Evidence.** `part2 AW-135, AW-136`
**Recurrence.** 2 items, 7 sightings — a race, so it appears whenever a slow query precedes a fast one.
**Impact.** Produces wrong output SILENTLY — the caller cannot detect it; the answer is plausible and about the right topic.
**Action.** Correlate strictly on request id; on mismatch, block instead of returning the best available payload.
**Priority.** NEXT
**Members.** AW-135, 136

### 16. One event envelope, delivered to every consumer

**What happens.** Six egress families emit lifecycle events and the hook system subscribes to two of them, so `worker_partial_result` is *always* dropped and the panel is empty after F5 because nothing re-fired the events it renders. Each family has its own field names for the same concepts.
**Evidence.** `part2 AW-144, AW-145, AW-146, AW-147, AW-148, AW-149, AW-150`
**Recurrence.** 7 items, 14 sightings.
**Impact.** Produces wrong output SILENTLY — observability reports "nothing happened" during activity.
**Action.** One event type with one discriminator; every egress publishes that type; a test that asserts each consumer received at least one event per lifecycle transition.
**Priority.** NEXT
**Members.** AW-144, 145, 146, 147, 148, 149, 150

### 17. Gates must fail closed, and say what they did

**What happens.** The permission gate returns `None` to mean *allowed*, so every unhandled path allows; one message interpolates a literal `{ws_id}` instead of the value, making the refusal unactionable; and a policy read an in-memory mirror instead of the store, which produced a **total `git_read` outage** — the guard refused everything until the mirror was refreshed. The record's own summary: "a guard that ranks axes will silently ACCIDENTALLY ALLOW".
**Evidence.** `part2 AW-151, AW-152, AW-168, AW-169; part3 AW-249 (fixed)`
**Recurrence.** 4 live items + 5 sightings; low frequency, unbounded blast radius.
**Impact.** Blocks work when it fails, permits work when it should not — both silent.
**Action.** Every gate returns an explicit decision object (allow/deny + reason + the value it evaluated); no `Optional[reason]`; read policy from the store, never a mirror; a `{ws_id}`-style test that greps emitted messages for unformatted braces.
**Priority.** NEXT
**Members.** AW-151, 152, 168, 169, 249 (fixed)

### 18. Record every dispatch, not just the one that has a job id

**What happens.** `job_status` only tracks queries submitted through `submit_query`, so spawns, execs and re-dispatches leave no record to inspect afterwards — which is why half the items in this document had to be reconstructed from transcripts.
**Evidence.** `part1 AW-079`
**Recurrence.** 1 item, 3 sightings — but it is the reason recurrence is under-counted (§6).
**Impact.** Costs time — every investigation starts from a transcript instead of a ledger.
**Action.** One append-only dispatch ledger: request id, task hash, effective budget (#5), parent, outcome.
**Priority.** NEXT
**Members.** AW-079

### 19. Counters and notifications must not lie

**What happens.** A `_with_mode` trailer appended to every command breaks `wc -l` on the output it just produced; an append operation reports "1 line(s) added" for multi-line content; and a one-character config difference (`.../v1/` vs `.../v1`) emits a phantom `Configuration updated` notice on every run.
**Evidence.** `part1 AW-063, AW-072, AW-078; part2 AW-175`
**Recurrence.** 4 items, two parts, 8 sightings.
**Impact.** Cosmetic individually, *produces wrong output SILENTLY* in aggregate — agents script against these numbers.
**Action.** Machine-readable metadata on a separate channel from payload; counts computed from what was written, not from the arguments.
**Priority.** MONITOR
**Members.** AW-063, 072, 078, 175

### T5 — Truthfulness: claims, evidence, and tests that cannot fail

Six problems, and the most expensive theme in the record. Every other theme is a defect in the machinery; this one is a defect in what the machinery reports about itself, which is why it survived so long.

### 20. Report only what you verified — and name the check

**What happens.** Head and branch claims are routinely written from memory or from a stale context: a report stated `HEAD=7f133bd` where the actual head was `63a2981`; a worker claimed it was on `feat/resource-permission-separation@cfde827` while actually on `dev@b27a360`; and one report asserted "Phase 2a: worker ran 0 tool calls" while quoting verbatim tool output.
**Evidence.** `part1 AW-022, AW-023, AW-024, AW-026; part2 AW-158, AW-159`
**Recurrence.** 6 items, two parts, 22 sightings — highest per-item recurrence in the document.
**Impact.** Produces wrong output SILENTLY — the report is confident, formatted, and wrong about the one thing that was checkable.
**Action.** Every externally-checkable fact in a report carries the command that produced it and its output; no sha, branch or count without one; unverified statements are marked unverified or removed.
**Priority.** ACT NOW
**Members.** AW-022, 023, 024, 026, 158, 159

### 21. Trace every claim to an artefact

**What happens.** Document ids `e831b50c3d6b` and `58d044093952` were cited in a report and do not exist; a `git diff` was claimed as evidence in a shell where `git` was not installed (#28).
**Evidence.** `part1 AW-025, AW-033`
**Recurrence.** 2 items, 5 sightings.
**Impact.** Produces wrong output SILENTLY — citations look like rigour and are unauditable.
**Action.** Ids and paths are produced by a lookup, never typed; a link that cannot be resolved at write time is an error, not a note.
**Priority.** NEXT
**Members.** AW-025, 033

### 22. A test that cannot fail is not evidence

**What happens.** Three distinct failure modes, all green: (a) a suite reported "352 passed" while 29 tests were being added, and the same suite later read 378 passed / 17 failed — the count moved, the verdict did not; (b) under `style=precise` an assertion was rewritten into a tautology; (c) reverting the fix left **every test green**. Meanwhile the full suite hangs at node #1382 and exits 124, so the number that gets quoted is always from a partial run.
**Evidence.** `part1 AW-027, AW-028, AW-030; part2 AW-153, AW-154, AW-155, AW-156; part3 AW-250, AW-251, AW-252, AW-253, AW-255, AW-256`
**Recurrence.** 13 items, all three parts, 28 sightings — the mechanism behind every other "fixed" claim in the record.
**Impact.** Produces wrong output SILENTLY — it certifies work that does not exist.
**Action.** RED-before for every fix (fail, then pass, recorded); mutation/revert check in the gate; forbid count-only reporting — a run is evidence only with the node list and the exit code; fix or quarantine the hanging node so a full run terminates.
**Priority.** ACT NOW
**Members.** AW-027, 028, 030, 153, 154, 155, 156, 250, 251, 252, 253, 255, 256

### 23. Ship the approved shape, not a scoped variant

**What happens.** A requested module was delivered as a single helper: `session/history_pruner.py` came back carrying only `_compact_turn`; an instruction to "remove the field entirely" produced a 150-line scoped variant that kept the field.
**Evidence.** `part3 AW-257, AW-258; part1 AW-029, AW-031`
**Recurrence.** 4 items, two parts, 7 sightings.
**Impact.** Costs time — the diff is reviewed twice and the caller inherits the difference.
**Action.** Restate the approved shape as an acceptance checklist in the brief (#1) and have the deliverable name each item; a partial delivery is reported as partial.
**Priority.** NEXT
**Members.** AW-029, 031, 257, 258

### 24. Exit status must survive the pipe

**What happens.** vitest printed `Errors 1 error` and exited 0, so the gate went green while the suite was failing.
**Evidence.** `part1 AW-032; part3 AW-254`
**Recurrence.** 2 items, 5 sightings.
**Impact.** Produces wrong output SILENTLY — CI is the last line of defence and it is not standing.
**Action.** Gate on parsed verdict *and* exit code, and fail the run when the two disagree; treat disagreement itself as a defect.
**Priority.** NEXT
**Members.** AW-032, 254

### 25. Don't declare done what you skipped

**What happens.** Phase-1 premise checks were never run and Phase 2 was delivered as success; a worker wrote a file inside a read-only audit it had been explicitly told not to modify; a "DONE" was reported from second-hand recon rather than from the work.
**Evidence.** `part1 AW-010; part2 AW-157, AW-161`
**Recurrence.** 3 items, two parts, 5 sightings.
**Impact.** Blocks work — the reopening cost is paid by whoever trusted the status.
**Action.** A phase report enumerates every step as done / skipped / not-attempted, with skipped steps named; "DONE" requires at least one artefact per step.
**Priority.** NEXT
**Members.** AW-010, 157, 161


### T6 — Sandbox and two trees: where the code actually is

Six problems. The stack has a tool tree and a container tree, and prose that describes each of them correctly but never says which one a given call touches — so each problem here is a case of an agent being right about the wrong tree.

### 26. Publish the worker's real permission envelope

**What happens.** Workers are handed the interactive agent's permission vocabulary and discover the differences at runtime: "ask requires interactive approval; not available in worker context", and a `filesystem:banned` state that reads as a policy rather than a mode. A session poisoned by the mismatch could not be cleared by `force=True` nor by restarting the machine.
**Evidence.** `part1 AW-043, AW-044, AW-045, AW-046; part2 AW-107, AW-108, AW-109; part3 AW-226 (fixed)`
**Recurrence.** 7 live items + fixed, two parts, 22 sightings — every worker meets it on its first denied write.
**Impact.** Blocks work — the agent retries a forbidden operation instead of a permitted equivalent.
**Action.** Emit the effective envelope (not the requested one) in the first worker response and in the brief; make `ask` resolve to a definite allow/deny in worker context rather than an error; state clearly what `force` does *not* clear.
**Priority.** ACT NOW
**Members.** AW-043, 044, 045, 046, 107, 108, 109, 226 (fixed)

### 27. One canonical path per file

**What happens.** The host root and `/workspace` name the same file and nothing says which is authoritative: a claim that "`make_system_notice()` does not exist" was true of the tree that was edited and false of the tree that was asked about; the prose warning about the two trees inverted which one was authoritative; and `FilePreview` refuses a `/workspace/...` path that other tools accept.
**Evidence.** `part1 AW-040, AW-047, AW-061; part3 AW-228, AW-229, AW-230, AW-231, AW-232, AW-233`
**Recurrence.** 9 items, two parts, 18 sightings — and it silently corrupts any cross-tree conclusion.
**Impact.** Produces wrong output SILENTLY — conclusions about the code are conclusions about whichever tree was read.
**Action.** Canonicalise at the boundary (one function, one prefix, one refusal message), have every read/write/preview tool accept the same path grammar, and stamp the resolved absolute path into tool output.
**Priority.** ACT NOW
**Members.** AW-040, 047, 061, 228, 229, 230, 231, 232, 233

### 28. Say out loud that the sandbox has no git

**What happens.** `git` exits 127 inside the execution container while the `git_*` tools work fine — by design. Nothing in the surface states it, so agents write shell pipelines that end in `git: command not found` and then report the tool-layer result as if it came from the shell.
**Evidence.** `part1 AW-034; part3 AW-235`
**Recurrence.** 2 items, 12 sightings — high recurrence from a two-line documentation gap.
**Impact.** Costs time — every rediscovery costs a failed command plus a re-plan.
**Action.** WONTFIX-DOCUMENT: keep the design, put one line in every sandbox-facing tool description and in the environment preamble: "no `git` binary in the container; use `git_*` tools".
**Priority.** WONTFIX-DOCUMENT
**Members.** AW-034, 235

### 29. Treat `/tmp` as scratch, not storage

**What happens.** `/tmp` is a 64 MB tmpfs inside a fresh container per call, while the workspace has 368 GB and persists; artefacts written to `/tmp` therefore either hit the cap or evaporate between calls.
**Evidence.** `part1 AW-035, AW-036; part2 AW-160, AW-171; part3 AW-234`
**Recurrence.** 5 items, three parts, 13 sightings.
**Impact.** Costs time — work is repeated, or the artefact simply is not there when the next call looks.
**Action.** State size and lifetime in the tool description; make the workspace path the documented default and fail loudly when a `/tmp` write exceeds a stated fraction of the cap.
**Priority.** NEXT
**Members.** AW-035, 036, 160, 171, 234

### 30. Make the sandbox capable, or declare what it cannot run

**What happens.** The image has no `node`/`npm` (a caller saw exit 124 and read it as a timeout), `/var/tmp` is read-only so a tool produced `ENOENT mkdir '/var/tmp/<rand>/web'` *before* collection began, and `ContainerExec`'s 30 s default kills the container mid-command.
**Evidence.** `part1 AW-037, AW-038, AW-039; part2 AW-172, AW-173; part3 AW-214, AW-236`
**Recurrence.** 7 items, three parts, 15 sightings.
**Impact.** Blocks work — whole toolchains are unreachable from the sandbox with no error that says so.
**Action.** Publish a capability manifest (binaries, writable roots, default timeout) and have the runner check the requested binary against it before starting; raise or document `ContainerExec`'s default so it does not become a silent kill (#5).
**Priority.** NEXT
**Members.** AW-037, 038, 039, 172, 173, 214, 236

### 31. Put durable output where git can see it

**What happens.** `Respond(report_body=...)` writes into `reports/`, which is gitignored — so the deliverable of a unit of work is invisible to `git status` and to any review; new knowledge-base files are invisible for the same reason unless the ignore rules carry an explicit `!` negation.
**Evidence.** `part1 AW-041, AW-042, AW-065, AW-066`
**Recurrence.** 4 items, 9 sightings.
**Impact.** Produces wrong output SILENTLY — the artefact exists and the record says nothing was produced.
**Action.** Write deliverables to a tracked path, or un-ignore the deliverable directory explicitly; a completeness check that the output path is visible to `git status` before reporting success.
**Priority.** NEXT
**Members.** AW-041, 042, 065, 066

### T7 — Git surface: parameters that are accepted and ignored

Six problems. The git tools are the most-used read surface in the record and the ones with the largest gap between the advertised signature and the executed behaviour.

### 32. Honour the parameters you accept

**What happens.** The signature promises filtering that the implementation drops: `log` ignores `ref` and `commit`, `show_file` ignores line ranges and truncates at ~10k tokens, and `diff` with a **list** `file_path` returns empty **silently** rather than diffing the listed paths. `blame` already honours the same parameters — so the wiring exists and was never extended, a one-line change per site.
**Evidence.** `part1 AW-048, AW-049, AW-050, AW-051, AW-052, AW-053, AW-054; part3 AW-237, AW-238, AW-239, AW-240, AW-248`
**Recurrence.** 12 items, two parts, 30 sightings — near-universal, because agents assume the signature is the contract.
**Impact.** Produces wrong output SILENTLY — "no changes" and "no commits" are answers, and they are false.
**Action.** Wire every declared parameter at every site (blame is the reference implementation), and make an unhonoured parameter raise rather than fall through to the default.
**Priority.** ACT NOW
**Members.** AW-048, 049, 050, 051, 052, 053, 054, 237, 238, 239, 240, 248

### 33. Never return an empty answer for a question you could not answer

**What happens.** Truncation is marked *inside* the returned content, so the tail reads as EOF and a caller concludes the file ended; and `diff` on an untracked file returns EMPTY, which reads as "no changes" when the true answer is "this file is not in the index".
**Evidence.** `part1 AW-064; part3 AW-241, AW-267`
**Recurrence.** 3 items, two parts, 5 sightings.
**Impact.** Produces wrong output SILENTLY — the two most dangerous words in the record are "no diff".
**Action.** Distinguish absent from empty in the return type (explicit `truncated` flag outside the payload; `not_indexed` for untracked paths), and never let a marker live in the content.
**Priority.** ACT NOW
**Members.** AW-064, 241, 267

### 34. Read the ref you mean, not the checkout

**What happens.** A read operation answers about the working checkout when the question was about a named ref, so the answer is right about the wrong revision.
**Evidence.** `part1 AW-062`
**Recurrence.** 1 item, 3 sightings — same family as #32; kept separate because the fix is semantic, not plumbing.
**Impact.** Produces wrong output SILENTLY.
**Action.** Resolve the ref explicitly, and stamp the resolved commit into the response so the caller can see which revision answered.
**Priority.** NEXT
**Members.** AW-062

### 35. Make writes verifiable and reversible

**What happens.** A branch was silently reset to `origin/dev`, destroying an unpushed commit; there is no fetch, merge, rebase or amend; and detached HEAD is now gated at three sites — a patch applied per symptom rather than to the operation model.
**Evidence.** `part1 AW-055, AW-056, AW-057, AW-058, AW-059; part3 AW-242, AW-243, AW-268 (fixed)`
**Recurrence.** 8 items, two parts, 17 sightings.
**Impact.** Blocks work — the failure mode is lost commits, which is the one thing the surface must never do.
**Action.** Refuse history-rewriting or ref-moving operations unless the previous ref is recorded and restorable; add the missing verbs; replace the three ad-hoc gates with one precondition check.
**Priority.** NEXT
**Members.** AW-055, 056, 057, 058, 059, 242, 243, 268 (fixed)

### 36. Reclaim what the surface created

**What happens.** Worktrees accumulate with no `rm` or prune path — 10 orphaned worktrees, ~232 MB, from a handful of operations.
**Evidence.** `part1 AW-060; part2 AW-166`
**Recurrence.** 2 items, 2 sightings.
**Impact.** Costs time — disc-bound, and stale worktrees confuse later path resolution (#27).
**Action.** Bind worktree lifetime to the unit of work: create → use → remove, with a sweep for orphans.
**Priority.** MONITOR
**Members.** AW-060, 166

### 37. Commit what was enumerated

**What happens.** A commit operation commits a subset of the paths it was given, so an enumerated change set lands partially and the remainder is indistinguishable from a deliberate omission.
**Evidence.** `part1 AW-067; part3 AW-246`
**Recurrence.** 2 items, 7 sightings.
**Impact.** Produces wrong output SILENTLY — the commit message describes more than the commit.
**Action.** Commit the enumerated set or fail; report the exact path list committed.
**Priority.** MONITOR
**Members.** AW-067, 246

### T8 — Read, search and the knowledge base

Seven problems. These are the tools an orchestrator uses to *find out* what is true, so a defect here propagates into every conclusion drawn elsewhere in this document.

### 38. Give the orchestrator a read primitive

**What happens.** There is no plain file-read on the orchestrator's surface beyond a bounded preview, so reading one 250-line newly written file required `stage`-ing it and reading it back through `diff_cached` — mutating the index to read is the recorded workaround, and it is permanent rather than situational.
**Evidence.** `part1 AW-080; part3 AW-244, AW-245, AW-247`
**Recurrence.** 4 items, two parts, every unit of work.
**Impact.** Blocks work — verification of a worker's output depends on a mutating workaround.
**Action.** Add `read_file(path, start, end)` at the orchestrator surface with no index side effects, and stop documenting the stage/diff dance.
**Priority.** ACT NOW
**Members.** AW-080, 244, 245, 247

### 39. Search must return matches, or say why it cannot

**What happens.** `query="worker"` returned ~352,940 tokens — an unbounded result set treated as an answer; "No matches found" is returned spuriously when `show_line_numbers=false, max_results=0`; and one search returned 1 match for a heading that occurs 13 times.
**Evidence.** `part1 AW-068, AW-075`
**Recurrence.** 2 items, 9 sightings — the first is a blow-up, the others are silent under-reporting.
**Impact.** Produces wrong output SILENTLY — "not found" is indistinguishable from "not searched".
**Action.** Cap results with an explicit `truncated`/`total_matches` pair (§33), and make parameter combinations that would produce a false negative a validation error.
**Priority.** ACT NOW
**Members.** AW-068, 075

### 40. Honour read ranges

**What happens.** Range arguments are accepted and ignored on read paths, so a caller asking for lines N–M receives the head of the file and concludes the rest is missing.
**Evidence.** `part1 AW-076`
**Recurrence.** 1 item, 5 sightings — same plumbing failure as #32, different surface.
**Impact.** Produces wrong output SILENTLY.
**Action.** Same wiring pass as #32; one range parser, applied at every read site.
**Priority.** NEXT
**Members.** AW-076

### 41. Make the knowledge base safe to append to

**What happens.** Appends are unbounded and unguarded: `bugs_and_fixes.md` reached 277,081 B before anything complained, and one `mode=summary` call raises `list.append() takes exactly one argument (4 given)` — a Python-level bug reached from a tool call.
**Evidence.** `part1 AW-069, AW-070, AW-071, AW-074`
**Recurrence.** 4 items, 11 sightings.
**Impact.** Blocks work — the append that keeps the record is the append that fails.
**Action.** Fix the append call; enforce a stated per-file size with rotation; validate `mode` against an enum instead of dispatching on a string.
**Priority.** NEXT
**Members.** AW-069, 070, 071, 074

### 42. Resolve knowledge-base domains unambiguously

**What happens.** Documents live in overlapping domains with no rule for which one wins, so the same query can be answered from two places with different content and no way to know which was authoritative.
**Evidence.** `part1 AW-073`
**Recurrence.** 1 item, 3 sightings.
**Impact.** Costs time — every lookup becomes a judgement call.
**Action.** One precedence rule, applied at lookup and stated in the result (which domain answered).
**Priority.** NEXT
**Members.** AW-073

### 43. Make working documents addressable and durable

**What happens.** Working documents have no stable address and no guaranteed lifetime, so an agent cannot cite one it wrote, and a later agent cannot rely on one it read.
**Evidence.** `part1 AW-077`
**Recurrence.** 1 item, 4 sightings.
**Impact.** Costs time — shared state degrades to copy-paste through the transcript.
**Action.** Stable id, explicit scope, and a lifetime tied to the unit of work rather than the process.
**Priority.** NEXT
**Members.** AW-077

### 44. The record of the traps inherits the traps

**What happens.** The knowledge base that documents this very machinery exhibits the same defects: 19 of 31 backticked paths in it no longer resolve; a heading pattern `## 2026-10-29 — ## Real Title` doubles ~61 headings; and a committed merge-conflict block sits at `.gitignore` L301–304.
**Evidence.** `part1 AW-072, AW-081; part2 AW-176; part3 AW-260, AW-261, AW-262`
**Recurrence.** 6 items, three parts, 8 sightings.
**Impact.** Cosmetic on the surface, *costs time* in practice — the reference is unreliable exactly where it is trusted.
**Action.** A lint pass over the KB in CI (path resolution, heading shape, conflict markers) run on every change; it is cheap and it is the same class of check the tools should have had.
**Priority.** MONITOR
**Members.** AW-072, 081, 176, 260, 261, 262

### T9 — Instances: identity, force, decay, parallelism

Five problems. Workers are referred to by name and handled as resources, and both halves of that sentence are wrong.

### 45. Address an instance, not a name

**What happens.** Names are not identities: a spawn returned `{"status": "stopped"}` and the worker executed **zero** tool calls while the caller believed work had started; four spawns left five idle instances; and lookups raise `_WorkerAmbiguityError` when a name matches more than one live instance.
**Evidence.** `part1 AW-004, AW-007; part2 AW-125, AW-126; part3 AW-222, AW-225`
**Recurrence.** 6 items, three parts, 12 sightings.
**Impact.** Blocks work — the caller cannot tell "running" from "stopped" from "never started".
**Action.** Return an instance id on spawn, require it for every subsequent operation, and treat a name as a query that may return many; a spawn that returns without an id is an error.
**Priority.** ACT NOW
**Members.** AW-004, 007, 125, 126, 222, 225

### 46. `force` must mean a clean slate

**What happens.** `force=True` does not clear the conditions it is meant to clear (see also the unresolvable session in #26), and a stale permission footprint survives the reset, so the "fresh" instance inherits the old one's restrictions.
**Evidence.** `part3 AW-220, AW-221`
**Recurrence.** 2 items, 3 sightings.
**Impact.** Blocks work — the only escape hatch from #26 does not open.
**Action.** Define `force` as an enumerated reset list, execute it, and return what was reset; anything not on the list is documented as not reset.
**Priority.** NEXT
**Members.** AW-220, 221

### 47. Long-lived instances degrade

**What happens.** After ~200 tool calls an instance returns a one-line fragment with `status: null` — consistent with context exhaustion, but reported as a normal response, so the caller only notices by the content being wrong.
**Evidence.** `part1 AW-016`
**Recurrence.** 1 item, 2 sightings — low count, certain mechanism.
**Impact.** Produces wrong output SILENTLY.
**Action.** Detect the degradation (#12, #14) and return a typed terminal state rather than a short answer; cap instance lifetime with a documented handoff.
**Priority.** MONITOR
**Members.** AW-016

### 48. Parallelism needs isolation

**What happens.** Concurrent workers share their write path, so a half-written file was read as content by another worker — a torn read presented as a complete document.
**Evidence.** `part3 AW-223`
**Recurrence.** 1 item, 1 sighting — rare, and unfalsifiable from the output alone.
**Impact.** Produces wrong output SILENTLY.
**Action.** Write-then-rename per worker, or a per-instance scratch area; never read a file another instance is writing.
**Priority.** MONITOR
**Members.** AW-223

### 49. Retire the standing workaround list

**What happens.** A list of standing workarounds is carried in the documentation instead of being fixed, so each entry is a defect that has been normalised — agents now learn the workaround before they learn the behaviour.
**Evidence.** `part2 AW-164`
**Recurrence.** 1 item, 3 sightings — but it is the mechanism that keeps everything else in this document alive.
**Impact.** Costs time — permanent tax, invisible on any single run.
**Action.** WONTFIX-DOCUMENT: either fix an entry or move it into this report as a numbered problem; a workaround list that neither shrinks nor is filed is a ledger of accepted defects.
**Priority.** WONTFIX-DOCUMENT
**Members.** AW-164


## 3. PRIORITY TABLE

Sorted by priority band, then by recurrence (highest first) within the band.

| Priority | # | Problem | Theme | Recurrence | Impact class | Size |
|---|---|---|---|---|---|---|
| ACT NOW | 5 | Declare one clock, and make every layer read that one | T2 wall clock | 40 | wrong output SILENTLY | L |
| ACT NOW | 14 | One meaning for `status`/`confidence`/`timeout_triggered` | T4 envelope | 30 | wrong output SILENTLY | M |
| ACT NOW | 32 | Honour the parameters you accept (git) | T7 git | 30 | wrong output SILENTLY | M |
| ACT NOW | 22 | A test that cannot fail is not evidence | T5 truthfulness | 28 | wrong output SILENTLY | L |
| ACT NOW | 1 | Deliver the whole brief — every key, unaltered | T1 dispatch | 26 | blocks work | L |
| ACT NOW | 20 | Report only what you verified | T5 truthfulness | 22 | wrong output SILENTLY | M |
| ACT NOW | 26 | Publish the worker's real permission envelope | T6 sandbox | 22 | blocks work | M |
| ACT NOW | 27 | One canonical path per file | T6 sandbox | 18 | wrong output SILENTLY | M |
| ACT NOW | 11 | Revocation must not land mid-edit | T3 ceilings | 15 | blocks work | M |
| ACT NOW | 45 | Address an instance, not a name | T9 instances | 12 | blocks work | M |
| ACT NOW | 39 | Search must return matches, or say why it cannot | T8 read/search | 9 | wrong output SILENTLY | S |
| ACT NOW | 6 | Never discard a payload that finished | T2 wall clock | 7 | blocks work | M |
| ACT NOW | 33 | Never return empty for a question you could not answer | T7 git | 5 | wrong output SILENTLY | S |
| ACT NOW | 38 | Give the orchestrator a read primitive | T8 read/search | permanent | blocks work | M |
| NEXT | 7 | Make stop, kill and pause mean something | T2 wall clock | 20 | blocks work | L |
| NEXT | 35 | Make git writes verifiable and reversible | T7 git | 17 | blocks work | L |
| NEXT | 3 | Make the documented calling convention the real one | T1 dispatch | 15 | costs time | S |
| NEXT | 30 | Make the sandbox capable, or declare what it cannot run | T6 sandbox | 15 | blocks work | M |
| NEXT | 16 | One event envelope, delivered to every consumer | T4 envelope | 14 | wrong output SILENTLY | M |
| NEXT | 9 | Checkpoint deterministically; make pause/resume stick | T2 wall clock | 14 | wrong output SILENTLY | M |
| NEXT | 29 | Treat `/tmp` as scratch, not storage | T6 sandbox | 13 | costs time | S |
| NEXT | 28 | Say out loud that the sandbox has no git | T6 sandbox | 12 | costs time | S |
| NEXT | 2 | Keep the brief alive for the whole unit of work | T1 dispatch | 11 | blocks work | M |
| NEXT | 12 | Make the ceiling a number, not folklore | T3 ceilings | 11 | wrong output SILENTLY | S |
| NEXT | 41 | Make the knowledge base safe to append to | T8 read/search | 11 | blocks work | M |
| NEXT | 4 | Verify the brief's premises before dispatch | T1 dispatch | 10 | costs time | M |
| NEXT | 8 | Stop promising background work you do not perform | T2 wall clock | 9 | costs time | S |
| NEXT | 31 | Put durable output where git can see it | T6 sandbox | 9 | wrong output SILENTLY | S |
| NEXT | 23 | Ship the approved shape, not a scoped variant | T5 truthfulness | 7 | costs time | M |
| NEXT | 15 | Never answer the previous query | T4 envelope | 7 | wrong output SILENTLY | S |
| NEXT | 37 | Commit what was enumerated | T7 git | 7 | wrong output SILENTLY | S |
| NEXT | 17 | Gates must fail closed, and say what they did | T4 envelope | 5 | blocks work | S |
| NEXT | 21 | Trace every claim to an artefact | T5 truthfulness | 5 | wrong output SILENTLY | S |
| NEXT | 25 | Don't declare done what you skipped | T5 truthfulness | 5 | blocks work | S |
| NEXT | 40 | Honour read ranges | T8 read/search | 5 | wrong output SILENTLY | S |
| NEXT | 24 | Exit status must survive the pipe | T5 truthfulness | 5 | wrong output SILENTLY | S |
| NEXT | 43 | Make working documents addressable and durable | T8 read/search | 4 | costs time | S |
| NEXT | 46 | `force` must mean a clean slate | T9 instances | 3 | blocks work | S |
| NEXT | 34 | Read the ref you mean, not the checkout | T7 git | 3 | wrong output SILENTLY | S |
| NEXT | 42 | Resolve knowledge-base domains unambiguously | T8 read/search | 3 | costs time | S |
| NEXT | 18 | Record every dispatch, not just the one with a job id | T4 envelope | 3 | costs time | S |
| MONITOR | 19 | Counters and notifications must not lie | T4 envelope | 8 | cosmetic | S |
| MONITOR | 44 | The record of the traps inherits the traps | T8 read/search | 8 | cosmetic | S |
| MONITOR | 13 | Prove the mechanism before shipping the fix | T3 ceilings | 4 | costs time | S |
| MONITOR | 10 | Orchestrator turns must not force mid-unit handoff | T2 wall clock | 3 | costs time | S |
| MONITOR | 36 | Reclaim what the surface created | T7 git | 2 | costs time | S |
| MONITOR | 47 | Long-lived instances degrade | T9 instances | 2 | wrong output SILENTLY | S |
| MONITOR | 48 | Parallelism needs isolation | T9 instances | 1 | wrong output SILENTLY | S |
| WONTFIX-DOCUMENT | 28 | Say out loud that the sandbox has no git | T6 sandbox | 12 | costs time | S |
| WONTFIX-DOCUMENT | 49 | Retire the standing workaround list | T9 instances | 3 | costs time | S |

**Sequencing.**

1. **#5 first, alone.** It is the highest-recurrence item in the record, it is a precondition for #14 (`timeout_triggered` needs the clock that fired) and #6 (a payload needs a boundary to be rescued at), and every later duration measurement is meaningless until there is one clock.
2. **Then #22, because it gates the rest.** No fix listed here can be shown to work while a revert leaves the suite green and a partial run quotes a count; land RED-before, the revert check and the hanging node before believing anything else on this page.
3. **Then the four silent-wrongness pairs, which are cheap and independent:** #32+#33+#40 (one wiring pass over the git/read surfaces, `blame` as the reference), #27 (one path canonicaliser), #14 (one envelope constructor), #20 (evidence-or-nothing in reports).
4. **#1 and #45 are one change in spirit** — the payload and its identity travel together — and together they remove the two ways a unit silently does nothing.
5. **Everything in NEXT with recurrence ≥ 10 is defensible as "the second wave"**; the MONITOR band is where defects go when nobody has been hurt by them yet, not where they are safe.

## 4. ALREADY FIXED

Six fixes are in the record with a residual worth keeping. Read this before filing anything in §2 as new.

1. **Soft-timeout comparison — fixed** (`part2 AW-112`). The soft-timeout never fired because the code compared a lowercase `'critical'` against a stored `'CRITICAL'`; the comparison is now case-correct. *Residual:* it was found by reading, not by a failing test (#22) — nothing prevents the next case-sensitivity mismatch.
2. **Timeout precedence — fixed** (`part2 AW-113`). D1/D2 ordering plus `QUERY_WAIT_GRACE_SECONDS = 60` now define which limit wins a race. *Residual:* the grace constant is a fourth number in a system that already had three (#5, #12) and is not surfaced in the envelope (#14).
3. **Pause lets the in-flight turn finish — fixed** (`part2 AW-121`). `_pause_reported` records the pause, with 2/2 RED-before evidence. *Residual:* pause is honest about the current turn and still not durable across instances (#9); the WLM machinery that would make it safe still ships off (#7).
4. **Compaction on stop paths — fixed** (`part2 AW-132`, F3). Stop paths now compact rather than truncate. *Residual:* what survives compaction is still allowed to include the brief itself (#2).
5. **Effective-permission mirror — fixed** (`part3 AW-226`). Three sites now read `effective_permissions` instead of the requested set. *Residual:* the envelope is fixed, the *publication* of it is not (#26) — workers still discover it by denial.
6. **Resource-container disk policy — fixed** (`part3 AW-249`). `use_disk=True` at three call sites plus `tests/test_resource_container_disk_policy.py`. *Residual:* the regression that motivated it was a policy read from an in-memory mirror (#17); the specific case is fixed, the pattern is not.
7. **Minor harness housekeeping — fixed** (`part3 AW-264`, `AW-265`, `AW-268`): queue-register/rollback and cancellation bookkeeping, and the detached-HEAD gates. *Residual:* detached HEAD is now gated at three ad-hoc sites rather than in the operation model (#35).

**Read this list sceptically.** Four of the seven were verified by reading code and one by a test file that also had to be written for the purpose; only #3 carries explicit RED-before evidence. That is the pattern §5 is about.

## 5. HONEST META-POINT

The strongest example in this record of a fix that was written down and never applied is **#32**: `blame` honours `ref`, `commit` and line ranges, `log` and `show_file` sit in the same module with the same signature and ignore them, and `diff` silently returns empty for a list-valued `file_path`. The wiring is a one-line change per call site. It has been known for the entire span of this record, it recurred 30 times across two parts, and the surface still accepts parameters it does not honour.

It is not an isolated case. It is the shape of the whole document: **the record knows more than the code does.** `VL:L24` — the vetting log that is supposed to close these items — still records the dispatch envelope as **BROKEN** while four separate "fixes" to the envelope have been listed as landed. Either the log is stale or the fixes were not what they claimed; from inside this document there is no way to tell which, and that ambiguity is itself the finding.

Three consequences worth stating plainly:

- **The status vocabulary cannot distinguish "fixed" from "reported fixed."** §4's residuals are not padding; they are the pattern. #3 has RED-before evidence, the rest have prose.
- **Recurrence numbers here are floors.** An item counted once is an item someone happened to write down, not an item that happened once.
- **The most-recurring items are the least surprising.** #5, #14 and #32 are all "the declared contract is not the executed contract". No amount of per-symptom patching will fix a class of failure whose common cause is that nothing owns the contract.

The actionable meta-point: before the next fix lands, require the three artefacts this document kept finding missing — a failing test first, the instrumented number before and after (#13), and one `VL` line updated in the same change. If those three were mandatory, roughly a third of §2 would not exist.

## 6. WHAT WE COULD NOT SEE

- **This is a floor, not a census.** Every count in this document is a count of *reported* occurrences; the population of occurrences was never measured.
- **Archives were only partially read.** Several of the three parts' source archives were sampled rather than consumed end to end, so items behind an unread archive are absent, not refuted.
- **316 of 340 working documents were never read.** The unread majority may contain repeats (harmless) or distinct families (not harmless); we cannot say which.
- **`agent_worker_timeline.md` was never harvested.** The one artefact that would let recurrence be counted per incident rather than per report was out of scope for this pass.
- **No independent measurement was taken for this consolidation.** Every number is quoted from a part; where parts disagreed the resolution is named in the affected problem, not adjudicated by a new experiment.
- **Silence is not health.** Themes with low counts (T3, T9) are low because few agents got far enough to hit them — not because the machinery is sound.

