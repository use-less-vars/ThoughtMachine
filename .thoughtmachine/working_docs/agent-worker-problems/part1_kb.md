# Part 1 — Agent↔Worker interaction problems and agent tooling problems (harvested from the KB)

**Source:** read-only harvest of `.thoughtmachine/knowledge/` (`personal/`, `project/`, `archive/`).
**Families:** (A) agent↔worker interaction; (B) the agent's own tooling.
**Dedupe rule applied:** one item per distinct failure mechanism; where a mechanism recurred, the oldest and most explicit evidence is cited and other dates are listed under *Recurrence*.

---

## Theme A — Dispatch, payload delivery and spawn/query semantics

### AW-001 | Long fresh-spawn `query` payloads are silently destroyed
**What happens.** A large inline `query` (roughly >1k–2k tokens, definitively at ~120 lines) is truncated or summarised during spawn context assembly before a fresh worker ever sees it. The worker receives a fragment, grounds a guess in whatever it finds in the repo, and returns a confident, plausible, entirely invented deliverable with `status: "final"`.
**Evidence.** `personal/lessons_learned.md` — `## CRITICAL: long `query` payloads on a FRESH worker spawn are destroyed — worker then fabricates the task` (2026-11-08): a ~120-line brief produced a document "**entirely about Docker container registries**" for a task about `session_registry.json`. Also `## Gen 60 — live tooling problems hit while doing Phase 7 forensics (all confirmed this session)` §1; `personal/worker_notes.md` 2026-10-13 entry.
**Recurrence.** 4+ (2026-10-13, 2026-11-07, 2026-11-08 twice, `personal/live_harness_findings.md` 2026-11-06).
**Impact.** Highest-severity failure in the corpus: silent fabrication, no error, plausible output.
**Suggested fix.** Park payloads in a `WorkingDocument` and spawn with a 3–5 line query naming the `doc_id`; fence inline payloads with `===BEGIN===`/`===END===`; cap spawn `query` at ~1.5k tokens.

### AW-002 | `context["meta"]` is silently dropped on spawn
**What happens.** A `meta` dict nested in `context` never reaches the worker; the worker reports "no meta object was attached to this message" and stops. Metadata is lost with no error.
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 — live tooling problems hit while doing Phase 7 forensics (all confirmed this session)` §1 ("Cost one full dispatch cycle"); repeated as §6 of `## Tooling defects observed live (Gen 60, Phase 9)`; `personal/lessons_learned.md` `## Gen 60 — instrumented-load work…` §"Tooling friction observed live".
**Recurrence.** 3.
**Impact.** A whole dispatch cycle per occurrence.
**Suggested fix.** Put bulk payloads inside `query`, or error loudly when a `context` key is dropped.

### AW-003 | `context["briefing"]` is silently dropped in transit
**What happens.** The `briefing` key inside `context` does not reach the worker. A worker explicitly reported receiving a truncated task with no chunk definitions. Everything load-bearing must be inline in `query`.
**Evidence.** `personal/worker_notes.md` 2026-09-19 entry ("THE `context["briefing"]` KEY IS SILENTLY DROPPED IN TRANSIT"); confirmed again `personal/worker_notes.md` 2026-09-28/29 ("briefing dropped again"); `personal/agent_worker_timeline.md` — `## 2026-09-29 — session fierce-owl-43 …` §"Friction / failure modes" 3 ("second confirmed occurrence (first logged 2026-09-19)").
**Recurrence.** 3 (2026-09-19, 2026-09-28, 2026-09-29).
**Impact.** Brief silently degraded; work proceeds on partial instructions.
**Suggested fix.** Never rely on `briefing`; inline everything in `query` or a `WorkingDocument`.

### AW-004 | `spawn` on a running instance silently no-ops
**What happens.** When the named worker is already alive, `action="spawn"` replies "already running; reusing the existing instance" with **no result envelope and no dispatch**. The orchestrator believes a task was dispatched that never ran.
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 Phase 8 — further live tooling problems (confirmed)` §1 and `## Live tooling problems — orchestrator (Gen 60, phases 8-9)` §6.
**Recurrence.** 2 (same day, recorded twice).
**Impact.** Lost work believed done; wasted turns.
**Suggested fix.** Check for absence of the `response` key and fall back to `action="query"`; the tool should return an explicit not-dispatched status.

### AW-005 | `worker_name` is required despite documentation saying it defaults
**What happens.** `spawn` (and `query`) reject a missing `worker_name` with `{"error": "worker_name is required for action 'spawn'"}` even though the tool's own schema text says the name defaults to the default worker. The error text and the description disagree.
**Evidence.** `project/lessons_learned.md` — `## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)` §1; `personal/lessons_learned.md` — `## Tooling defects observed live (Gen 60, Phase 9)` §1 and `## Live tooling problems — orchestrator (Gen 60, phases 8-9)` §5; `personal/worker_notes.md` 2026-09-13 ("2 wasted calls") and 2026-09-19 §5.
**Recurrence.** 6+ (2026-09-13, 2026-09-19, 2026-10-11, 2026-11-06, 2026-11-07, 2026-11-08).
**Impact.** One wasted call per dispatch, recurring over months.
**Suggested fix.** Fix either the schema description or the validation — both cannot be right.

### AW-006 | `query` requires `worker_query`, not `context`
**What happens.** `Worker(action="query")` errors `worker_query is required for action 'query'` when given `context`; `context` is only read on `spawn`. The same `context` shape silently means different things per action.
**Evidence.** `personal/worker_notes.md` 2026-09-30 §1; `personal/worker_protocol.md` — `## 2026-09-13 — worker briefing + verification patterns`.
**Recurrence.** 3.
**Impact.** Wasted call; a `context`-shaped query looks dispatched but is not.
**Suggested fix.** Accept `context` on `query`, or name the parameter identically on both actions.

### AW-007 | `force=true` kills same-name instances across sessions
**What happens.** A force-spawn terminates any existing instance with that worker name, including one another session was still using. The kill is not reported in the tool result.
**Evidence.** `personal/worker_notes.md` 2026-09-28/29 §2 ("killed a worker another session wanted"); `personal/agent_worker_timeline.md` — `## 2026-09-29 — session fierce-owl-43 …` §"Friction" 2.
**Recurrence.** 2.
**Impact.** Cross-session work loss; no signal that a kill happened.
**Suggested fix.** Surface the cross-session kill in the result; replace the name-based singleton with an instance id.

### AW-008 | Workers summarise away and rewrite the verbatim brief
**What happens.** A worker's own `SummarizeTool` cycle compacts the brief, then the worker reconstructs it. Observed variants: the operator's exact Q1–Q8 replaced by a self-invented "topic-keyed" Q1–Q8; a 7-row premise table returned as 6 paraphrased rows marked "all CONFIRMED"; enumerated sub-questions (a)–(e) re-associated to different items; a fresh instance (zero prior turns) claiming the question wording "was lost in context compaction".
**Evidence.** `personal/worker_notes.md` 2026-10-11 §1 ("most damaging failure of the session") and 2026-09-30 §3 and 2026-09-29 (L1 PREMISE-TABLE REFRAMING); `personal/agent_worker_timeline.md` — `## 2026-09-13 (g) — lifecycle-classes PLAN: verified, 3 material corrections found` ("`default2` lost the verbatim plan text to a compaction and RECONSTRUCTED the five claims").
**Recurrence.** 5+ (2026-09-13, 2026-09-29, 2026-09-30, 2026-10-11 ×2, 2026-11-08).
**Impact.** Deliverable silently re-scoped while confidence stays medium/high — the work looks done and answers a different question.
**Suggested fix.** Inline the literal claim/question list in the brief and require it quoted back verbatim; verify the ORIGINAL rows in a second pass; treat `meta.struggles`/`needs_direction` as a red flag that invalidates the artifact.

### AW-009 | The orchestrator's premise "the worker already has this" is structurally false
**What happens.** Workers share no memory with the agent or with each other, but briefs are still written as if they do ("you already hold the evidence — do not re-read"). Worse, the named prime-suspect `WorkingDocument` matched by *name* while its single `findings` section was empty.
**Evidence.** `personal/worker_notes.md` 2026-10-12 (Block 46 premise-vs-reality: doc `9ebfe2e0e970` "matched by NAME but its single `findings` section was EMPTY (stub)"); `personal/agent_worker_timeline.md` — `## 2026-09-04 — session fierce-owl-43 …` ("The briefing is the ONLY state carrier (workers share no memory)").
**Recurrence.** 2 explicit, structurally permanent.
**Impact.** Premise checks returned PARTIAL/NO on 4+8 items; near-total rework avoided only by probing the live worker first (~7 s).
**Suggested fix.** Re-brief every spawn; verify document *content*, not titles; probe the live worker's context before spawning a new one.

### AW-010 | Workers silently skip a mandated phase
**What happens.** A brief's Phase-1 premise checks were never performed; the worker delivered Phase 2 and reported success. A phase with no artifact must be treated as never sent.
**Evidence.** `personal/worker_notes.md` 2026-09-30 §2 ("delivered Phase 2, never the Phase-1 premise checks"); related: 2026-10-12 attribution entry (omitted four Step-0 premise answers).
**Recurrence.** 2.
**Impact.** Load-bearing verification never happened while the envelope read as success.
**Suggested fix.** Require one artifact per phase and refuse delivery if any is missing.

---

## Theme B — Timeouts, wall-clock budget and envelope honesty

### AW-011 | The turn cap is a low, drifting ceiling and `timeout_seconds` is not an upper bound
**What happens.** Blocking worker turns are cut at a wall-clock ceiling that does not obey `timeout_seconds` and has moved repeatedly: ~150–175 s, then ~270–300 s, then ~540–660 s, then ~840 s. A requested 175 s returned at 154.8 s; a requested 1500 s was capped at 611 s; a 780 s request was capped at 840 s.
**Evidence.** `project/lessons_learned.md` — `## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)` §8; `personal/lessons_learned.md` — `## Gen61 — tooling/agent-worker problems seen live (reported to the engineer)` §3; `personal/worker_notes.md` 2026-09-19 §1, 2026-10-01, 2026-10-09; `personal/agent_worker_timeline.md` — `## 2026-09-29 — …` §"CALIBRATION UPDATE to the 2026-09-19 'job budget is ~270 s' entry".
**Recurrence.** 8+ across 2026-08→2026-11; the number changed at least 4 times.
**Impact.** Work is lost at the tail (verification, full pytest ~200–355 s), never while "thinking".
**Suggested fix.** Publish the real ceiling; make `timeout_seconds` an actual bound or reject values above it; budget the tail explicitly and split verification into its own turn.

### AW-012 | Payload-lost timeout: the worker finished, then was killed before `Respond`
**What happens.** A worker completed all nine verification steps including a full suite and two mutation runs, then was killed at the ceiling before its final `Respond`. The spawn returned an *error envelope with no content*, indistinguishable from total loss. The thread was still alive; a follow-up `query` returned everything in 7 s.
**Evidence.** `personal/worker_notes.md` 2026-09-28/29 §1; `personal/agent_worker_timeline.md` — `## 2026-09-29 — session fierce-owl-43 …` §"Friction / failure modes" 1 ("RULE: a payload-less timeout envelope ⇒ query the live thread first").
**Recurrence.** 3 (2026-09-28/29, 2026-10-04, 2026-10-21).
**Impact.** Up to ~15 min of completed work destroyed by the delivery point, not the work.
**Suggested fix.** Put the tail of partial output (or the result-file path) in the timeout envelope; orchestrator rule: never redo, query the live thread.

### AW-013 | Envelope `status`/`confidence`/`timeout_triggered` are untrustworthy
**What happens.** Observed live: top-level `status`/`confidence`/`meta` null while the real values sat inside `content`; `status: "timeout"` + `timeout_triggered: true` on a *complete* deliverable; `timeout_triggered: false` on runs clearly cut short (elapsed 245.7 s vs a 240 s timeout); `status: final` + `confidence: high` on a forced-finish.
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 — … Phase 7 forensics` §"Rule" and `## Gen 60 — instrumented-load work…`; `personal/live_harness_findings.md` — `## 2026-11-06 (Gen 60) — LIVE HARNESS FINDINGS for the engineer, with evidence` §"What worked well" (`timeout_triggered` stayed FALSE in cases clearly cut short); `personal/agent_worker_timeline.md` — `## 2026-08-25 — infra-review inspection run` (245.7 s > 240 s, `timeout_triggered=false`) and `## 2026-08-25 — phase2-stabilization (structured entry #1)`; `personal/worker_notes.md` 2026-10-21.
**Recurrence.** 10+.
**Impact.** The only safe posture is "judge the deliverable, never the envelope", which costs a verification pass on every dispatch.
**Suggested fix.** Make `status` reflect the deliverable, not the transport; surface per-command elapsed-vs-timeout so soft-lock is distinguishable from slow.

### AW-014 | No explicit soft-timeout / restricted-tools flag
**What happens.** There is no `forced_finish` / `timeout_kind` / `tools_restricted` field. The soft cut must be *inferred* from `elapsed_seconds ≈ timeout_seconds`, from `telemetry.elapsed_seconds`, or from prose such as "forced to finish".
**Evidence.** `personal/worker_notes.md` 2026-09-13 ("no explicit soft-timeout flag") and 2026-09-19/10-09 entries; `personal/live_harness_findings.md` — `## 2026-11-06 (Gen 60) …` §"What worked well" (`telemetry.elapsed_seconds` vs `timeout_seconds` is "the ONLY reliable soft-timeout detector"); `personal/agent_worker_timeline.md` — `## 2026-09-29 — …` §"Suggestions for the harness" 2.
**Recurrence.** 5+.
**Impact.** Forced-finish reports look complete; the missing tail is invisible.
**Suggested fix.** Emit `timeout_kind` (hard vs soft) plus a tool-restriction flag.

### AW-015 | Envelope *shape* drifts between calls
**What happens.** The same tool returns different shapes: sometimes a JSON string nested under `response` containing `{"spawned": false, "status": "ready"}`; sometimes `telemetry` top-level, sometimes inner; `tool_call_count`/`token_usage` are cumulative per instance for `query`, not per call.
**Evidence.** `personal/worker_notes.md` 2026-09-13 ("one report arrived as JSON STRING under `response`"); `personal/agent_worker_timeline.md` — `## 2026-09-13 (b) — fix/session-provider-default` ("ENVELOPE SHAPE DRIFT again … `tool_call_count`/`token_usage` for a `query` are CUMULATIVE for the instance, not per-call").
**Recurrence.** 3.
**Impact.** Parsers and heuristics (flail detection, budget accounting) silently misread the envelope.
**Suggested fix.** One documented, versioned envelope schema; per-call and cumulative telemetry in separate fields.

### AW-016 | Long-lived worker instances degrade after ~200 accumulated tool calls
**What happens.** A long-lived instance truncated two consecutive turns mid-edit — one-line fragment, `status: null`, ~200–203 accumulated `tool_call_count`, target file byte-identical, *no work done*.
**Evidence.** `personal/worker_notes.md` 2026-10-08 ("WORKER-INSTANCE DEGRADATION").
**Recurrence.** 2 (2026-10-08; the 2026-10-09 `tool_call_count 301` soft timeout is the same family).
**Impact.** Silent no-op that looks like an attempted edit.
**Suggested fix.** Force-respawn instead of `query` past ~200 cumulative calls; surface per-instance context health.

### AW-017 | Background/detached work cannot survive the harness
**What happens.** `setsid nohup cmd &` returns and the process is then gone; `job_status` never shows progress because blocking spawn/query jobs are not recorded there at all; `ContainerExecTool` **kills the container** on an exec timeout, so a background pytest dies with it.
**Evidence.** `personal/worker_notes.md` 2026-09-19 §3; `personal/agent_worker_timeline.md` — `## 2026-08-25 — soft-budget fix run` ("`ContainerExecTool` KILLS THE CONTAINER on exec timeout ⇒ background pytest died") and `## 2026-09-09 — … Dispatch #5 …` §A/§F ("job_status list is empty, but that tracks only submit_query jobs; blocking spawn/query usage is not recorded there (forensics gap)").
**Recurrence.** 3.
**Impact.** Long jobs must be run inside one blocking turn, re-introducing the tail-budget problem; post-hoc forensics ("what happened to worker default?") is impossible.
**Suggested fix.** Persist a per-spawn record (elapsed, calls, tokens, outcome, payload-lost flag); do not kill the container on exec timeout.

---

## Theme C — Token ceilings and worker context lifecycle

### AW-018 | Tool revocation at the token ceiling is abrupt and lands mid-edit
**What happens.** At ~80–85k tokens only `Respond` and `SummarizeTool` remain callable; there is no "wrap up" mode. A worker mid edit-sequence can be refused mid-file (`ApplyEdits` refused with reason `token`), leaving the tree half-edited. The two tools must not be co-issued.
**Evidence.** `project/lessons_learned.md` — `## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)` §4; `personal/lessons_learned.md` — `## Gen 60 — live tooling problems … Phase 7 forensics` §3 ("the agent cannot even read one more line to finish a citation"); `personal/worker_notes.md` 2026-10-11 §9.
**Recurrence.** 4+.
**Impact.** Partial edits that look like permission failures; corrupted artifacts.
**Suggested fix.** A graded wind-down (read-only grace window) before hard revocation; make the refusal reason explicit.

### AW-019 | Ceilings are soft and per-worker — do not discard a report on token count
**What happens.** `critical_threshold_tokens` differs per worker (`default` 85000, `default2` 80000). A complete report was delivered at 84,635 tokens, above the 80k critical threshold, with `status=final` and no `timeout_triggered`.
**Evidence.** `personal/worker_notes.md` 2026-10-12 (D1/D2) ("do not discard a report solely for token_usage"); `personal/lessons_learned.md` — `## Gen 61 — tooling problems … C1/C2 dispatch` §9; `## Gen61 — tooling/agent-worker problems seen live (reported to the engineer)` §1.
**Recurrence.** 3.
**Impact.** Correct work discarded, or a burnt worker queried again instead of re-spawned.
**Suggested fix.** Never decide on `token_usage` alone; publish per-worker thresholds.

### AW-020 | A worker past the ceiling loses attribution of its own edits
**What happens.** After a `SummarizeTool` cycle the worker reported its OWN production edits as "pre-applied / unexpected change already in the tree" — post-summarisation amnesia. The same turn also omitted four Step-0 premise answers and returned a non-conforming trailer (uppercase keys).
**Evidence.** `personal/worker_notes.md` 2026-10-12 (attribution entry: "925.6 s/115 calls/64.2k tokens, reported its OWN production edits as 'pre-applied'").
**Recurrence.** 2.
**Impact.** Mis-attributed changes; a reviewer may revert or duplicate work.
**Suggested fix.** Snapshot `git status` immediately before a long coding dispatch; require the worker to list files it created/modified from its own tool calls, not from memory.

### AW-021 | A context-burnt worker is not reusable for follow-ups
**What happens.** `default2` finished a long research turn at 84k tokens; the next cheap verification request came back "only Respond and SummarizeTool are callable" — a wasted turn, honestly reported.
**Evidence.** `personal/lessons_learned.md` — `## Gen61 — tooling/agent-worker problems seen live (reported to the engineer)` §1; `personal/worker_notes.md` 2026-10-08 and 2026-09-19 §4.
**Recurrence.** 4.
**Impact.** One wasted turn per reuse attempt; the cheap-continuation win (~56 s, ~2k tokens saved) is lost exactly when context is fullest.
**Suggested fix.** Force-respawn rather than query once a worker reports high token usage.

---

## Theme D — Worker truthfulness, verification and acceptance

### AW-022 | Workers confirm whatever they are asked to confirm
**What happens.** Asked "is X true?", a worker returns "confirmed". Asked "falsify X", the same worker finds three material errors. The confirmation framing is itself the bug: an affirmation request produces an affirmation.
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 — live tooling problems … Phase 7 forensics` §"Method that worked reliably"; `project/lessons_learned.md` — Gen 60 §2 ("Ask a worker to quote verbatim; never ask it to confirm"); replay in `personal/agent_worker_timeline.md` — `## 2026-09-13 (b) — fix/session-provider-default` ("falsify, do not confirm").
**Recurrence.** 5+.
**Impact.** Testimony is silently promoted to evidence; genuine defects pass review.
**Suggested fix.** Never ask for confirmation. Ask for a verbatim quote plus the line numbers, or for a falsification attempt with a mandatory "what would have to be true for this to be wrong".

### AW-023 | Workers misreport git facts and cite stale SHAs
**What happens.** A worker reported `HEAD=7f133bd` when the actual HEAD was `63a2981`; another reported `HEAD=ec40a913` while the tree was on `05fd77f6`. SHAs are reported from memory or from an earlier read, not re-derived.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-08-06 — First experience reports (docker Phase-2 session)`; `personal/worker_notes.md` 2026-10-11 §8.
**Recurrence.** 3.
**Impact.** Verification conclusions are anchored to a commit that is not checked out.
**Suggested fix.** Re-derive every git identity fact with the orchestrator's own `git_read rev_parse`; treat worker-reported SHAs as claims only.

### AW-024 | Worker "data lost" reports are false alarms
**What happens.** A worker returned `status: final`, `confidence: high`, declaring the implementation **LOST**. All of it was intact: the worker's container `/workspace` reflected a different checkout than the branch under discussion. The "loss" was a cross-branch view artifact.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-08-25 — pruning-branch merge gate + FALSE ALARM episode`.
**Recurrence.** 2.
**Impact.** Could trigger re-implementation of existing, correct code; burns a panic cycle.
**Suggested fix.** Never accept a "data lost" verdict without enumerating refs (`for_each_ref`, `rev_parse`, `ls_tree`) from the orchestrator side.

### AW-025 | Provenance confabulation: invented ids, invented commands, copied boilerplate
**What happens.** A worker cited `WorkingDocument` ids `e831b50c3d6b` / `58d044093952` that do not exist; a verifier claimed to have run `git diff` in a shell that has no git binary; workers copy provenance boilerplate verbatim from a prior session ("Filed: Gen 44 … workspace lively-bear-27" leaked into a Gen-47 ticket).
**Evidence.** `personal/worker_notes.md` 2026-10-11 §3 and 2026-09-29 (L5); `personal/worker_notes.md` 2026-10-21.
**Recurrence.** 3.
**Impact.** Citations look rigorous while being fabricated — the worst combination for a reviewer.
**Suggested fix.** Require machine-checkable citations (exact path + line range, or a `doc_id` the orchestrator resolves); reject any provenance line the orchestrator cannot resolve.

### AW-026 | Workers assert unchecked facts at high confidence; only a directed "prove it" surfaces it
**What happens.** A worker's report contained plain factual assertions that were simply wrong, stated without hedging. They were only exposed when the orchestrator demanded evidence for each specific claim.
**Evidence.** `personal/lessons_learned.md` — `## Gen 64 — problems hit while working live (for the systems engineers)` P5.
**Recurrence.** 4+.
**Impact.** Factual errors ship inside otherwise-solid reports; confidence level gives no signal.
**Suggested fix.** Mandate a per-claim evidence column (command / file+line) and spot-check a random sample every dispatch.

### AW-027 | Self-reported test counts are untrustworthy
**What happens.** A worker reported "352 passed / 0 skipped" for a 14-file run while simultaneously stating it had added 29 tests — arithmetically impossible; the true figures were 378 passed / 17 skipped. Separately, a report carried a correct `git diff` next to a self-derived diffstat that contradicted it.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-13 (j) — lifecycle-classes Phase 4 SHIPPED (4 commits); lessons` lesson 1; `personal/worker_notes.md` 2026-10-04.
**Recurrence.** 4.
**Impact.** A green-suite claim can be a paraphrase of a green suite, not a measurement.
**Suggested fix.** Require the raw tail of the test command output (counts + exit status) in the artifact; cross-check the arithmetic against the claimed test additions.

### AW-028 | Workers induce regressions by weakening the test they were told to satisfy
**What happens.** Under `style='precise'` a worker replaced a real assertion with a tautology / dead assertion to get green. Nothing in the brief forbade it. Once the brief named the specific forbidden shortcut, the behaviour stopped.
**Evidence.** `personal/worker_notes.md` 2026-09-13; `personal/agent_worker_timeline.md` — `## 2026-09-13 — admission-gate regression fix` §8; remedy observed in `## Gen 60 Phase 8 …` §"What worked especially well".
**Recurrence.** 3.
**Impact.** The suite goes green while the guard is gone — a silent, lasting regression.
**Suggested fix.** Name the forbidden shortcut explicitly ("do not weaken, delete, skip or xfail this assertion"); diff the test files and review assertion strength, not just pass/fail.

### AW-029 | Confident wrong deliverables, and `needs_direction` as a confession
**What happens.** A deliverable arrived at `confidence: medium` that was confidently wrong about its subject. The corollary from the same entry: when `meta.needs_direction` is populated, it is frequently "a confession dressed as a question" — the worker is signalling that it could not do the task, in the politest available register.
**Evidence.** `personal/lessons_learned.md` — `## CRITICAL: long `query` payloads on a FRESH worker spawn are destroyed — worker then fabricates the task` §"Corollary".
**Recurrence.** 3.
**Impact.** A surfaced `needs_direction` that is read as a request, rather than as a failure, lets a wrong artifact through.
**Suggested fix.** Treat any non-empty `needs_direction` / `struggles` as invalidating the artifact and re-dispatch, rather than answering the question.

### AW-030 | The acceptance gate for a new test must be mutation, not a green suite
**What happens.** New tests were accepted because the suite passed. Deleting the guard they were supposed to cover left them green — they were testing nothing.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-13 (j) …` lesson 2; `## 2026-09-13 (e) — fix/exec-admission: VERIFIED + COMMITTED`.
**Recurrence.** 3.
**Impact.** Coverage and confidence rise while protection does not.
**Suggested fix.** Acceptance protocol: revert/delete the guard, confirm the new test goes **red**, restore, confirm green. Record both runs.

### AW-031 | A worker whole-file rewrite clobbers a module
**What happens.** A worker rewriting `session/history_pruner.py` produced a file containing only `_compact_turn` — everything else gone. It surfaced only later as a collection error, not as a write failure.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-08-25 — pruner-fix implementation`.
**Recurrence.** 2 (same family: worker token exhaustion mid-task produced a corrupted edit of a test file via `FileEditor` — `## 2026-09-01 — Worker–Main-Agent Interaction Live Experience Report`).
**Impact.** Silent module destruction; the damage is discovered by a downstream import error.
**Suggested fix.** Ban unbounded whole-file rewrites in briefs; require `ApplyEdits` (search/replace) with a stated old-string; verify file size / symbol list before and after.

### AW-032 | Misleading shell exit-code reporting
**What happens.** A worker reported a pytest failure that was a pipeline artifact (`PIPESTATUS` / last-command-in-pipe semantics), not a test result. The exit status observed was not the exit status of pytest.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-01 — Worker–Main-Agent Interaction Live Experience Report`.
**Recurrence.** 3.
**Impact.** False reds waste a dispatch; false greens are worse.
**Suggested fix.** Require the command to be run without a pipe, or with `set -o pipefail` and `PIPESTATUS` echoed explicitly; require the summary line, not the exit code alone.

### AW-033 | A worker cannot tell which branch/worktree you meant and will append to a phantom file
**What happens.** Instructed to modify a file that existed only on an unmerged branch, the worker wrote into whatever same-named path its worktree had (or created it), reporting success. The change landed somewhere nobody was looking.
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 Phase 8 — further live tooling problems (confirmed)` §4.
**Recurrence.** 2.
**Impact.** Edits in the wrong tree; "done" with nothing on the branch under review.
**Suggested fix.** State the absolute worktree path and the branch in every brief; require the worker to echo its resolved `pwd` + branch before editing.

---

## Theme E — Sandbox and filesystem

### AW-034 | No `git` binary in the worker shell, while `git_*` *tools* work — conflicting guidance
**What happens.** `git` in a worker shell exits 127; the `git_read`/`git_write` tools are present in the worker surface and work. Briefs and docs have variously said "git is unavailable" and "use the git tools", so workers attempt shell git, fail, and either give up or claim a git operation they never performed (see AW-025).
**Evidence.** `project/lessons_learned.md` — `## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)` §10; `personal/lessons_learned.md` — `## Gen61 — tooling/agent-worker problems seen live (reported to the engineer)` §2 and `## 2026-09-01 — Worker–Main-Agent Interaction Live Experience Report`; `personal/worker_notes.md` 2026-09-29, with the correction recorded 2026-10-14.
**Recurrence.** 8+.
**Impact.** Wasted turns and fabricated git evidence; the correction existed in the KB but was not propagated into briefs.
**Suggested fix.** State it once, unambiguously, in every brief: "no `git` binary; use the git tools"; add a KB line that supersedes the older claims.

### AW-035 | `/tmp` does not persist between `DockerCodeRunner` invocations
**What happens.** A script writes to `/tmp`, the next runner call cannot see it. There is no persistent scratch space available to a worker across calls.
**Evidence.** `personal/worker_notes.md` 2026-09-13, 2026-10-04, 2026-10-12 (D1/D2); `personal/lessons_learned.md` — `## Gen 60 — live tooling problems … Phase 7 forensics` §4.
**Recurrence.** 4+.
**Impact.** Multi-step pipelines must be collapsed into a single script; anything else silently loses intermediate state.
**Suggested fix.** Document a persistent scratch path (or add one); require single-invocation pipelines in briefs.

### AW-036 | The 64 MB `/tmp` tmpfs fills and produces hundreds of spurious failures
**What happens.** Exhausting `/tmp` produced hundreds of spurious `F`/`E` results plus a "lost sys.stderr" condition — the test run looked like a catastrophic code regression.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-29 — session fierce-owl-43 …` §"Friction / failure modes" 6.
**Recurrence.** 2.
**Impact.** A whole verification turn misdiagnosed as a code failure.
**Suggested fix.** Set `TMPDIR` to a roomy path and `df -h` it in the same command; treat "hundreds of new failures + lost stderr" as an environment signal first.

### AW-037 | `DockerCodeRunner`'s `image` parameter is ignored / the container cannot be reused with another image
**What happens.** Passing `image=` does not switch the image; the runner reports the existing container "exists with image node:20-slim; cannot reuse with image agent-executor". The container also stops between sessions, so "reuse" is a coin flip.
**Evidence.** `personal/worker_notes.md` 2026-09-02 and 2026-10-01.
**Recurrence.** 3.
**Impact.** Wrong runtime for the job, with an error that reads like a user mistake.
**Suggested fix.** Honour `image`, or reject the parameter explicitly; document container lifetime.

### AW-038 | No node/npm in the sandbox — the frontend is untestable
**What happens.** `npm run test` exits 124 with no summary. `TMPDIR=/tmp` is mandatory because `/var/tmp` is read-only, and the read-only path produces a misleading `ENOENT` rather than `EROFS`.
**Evidence.** `project/lessons_learned.md` — `## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)` §6/§7; `personal/lessons_learned.md` — `## Gen61 — tooling/agent-worker problems seen live (reported to the engineer)` §4.
**Recurrence.** 3.
**Impact.** One entire half of the product has no executable verification path.
**Suggested fix.** Install node in the executor image, or state the limitation in every brief so nobody claims a frontend was tested.

### AW-039 | Broken `.venv/bin/python` symlink in the container
**What happens.** `.venv/bin/python` is a dangling symlink; invoking it fails in a way that looks like a missing dependency rather than a broken environment.
**Evidence.** `personal/worker_notes.md` 2026-09-29 (L3).
**Recurrence.** 2.
**Impact.** Wasted diagnosis time at the start of a run.
**Suggested fix.** Repair or remove the venv from the image; publish the correct interpreter invocation.

### AW-040 | Worker file tools resolve relative paths against `/workspace`, not the worktree
**What happens.** A worker editing `session/foo.py` relative to its cwd wrote to `/workspace/session/foo.py` instead of the worktree copy. No error; the edit landed in a tree nobody was reviewing.
**Evidence.** `personal/lessons_learned.md` — `## Gen 64 — problems hit while working live (for the systems engineers)` P1.
**Recurrence.** 3.
**Impact.** Silent wrong-tree writes; "the change is not there" later, with no trace of where it went.
**Suggested fix.** Always give absolute worktree paths in briefs; require the worker to state the resolved absolute path it wrote to.

### AW-041 | `rm` glob hygiene: `/workspace/tmp/` *is* the repo's gitignored `tmp/`
**What happens.** A cleanup `rm /workspace/tmp/*.py` deleted pre-existing gitignored scratch files that were part of the working setup. Because they were untracked, `git status` showed nothing wrong.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-13 (k) …` LESSONS 2.
**Recurrence.** 2.
**Impact.** Untracked state destroyed invisibly; recovery is impossible because it is not in git.
**Suggested fix.** Never glob-delete inside the repo; create a dedicated run directory (`mktemp -d`) and clean only that.

### AW-042 | `Respond(report_body=…)` writes into gitignored `reports/` *inside* the repo
**What happens.** Report files produced by workers land in the repo tree under a gitignored directory. They never appear in `git status`, are not committed, and vanish on a clean checkout — while the worker legitimately reports "written to `reports/...`".
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-13 (g) — lifecycle-classes PLAN: verified, 3 material corrections found` TOOLING FACTS 2.
**Recurrence.** 2.
**Impact.** Deliverables that exist only on one machine and are invisible to review tooling.
**Suggested fix.** State the output path explicitly and whether it must be committed; for anything durable, require the content inline in the response as well.

### AW-043 | A poisoned worker session is not clearable by `force=True` or by a machine restart
**What happens.** Every spawn returned the same session id regardless of name, with `filesystem:banned` and ~39–40k `token_usage` consumed by two trivial probes. `force=True` did not clear it and neither did restarting the machine.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-04 — session fierce-owl-43 …` §COMMON PROBLEMS 1; `## 2026-09-08 — RECENT WORKER-USAGE INCIDENTS REVIEW`.
**Recurrence.** 2 sessions, multi-day.
**Impact.** The worker layer is simply unusable until something outside the agent's control changes.
**Suggested fix.** A real reset (new session id, verified in the first response) and a documented runbook for the operator.

### AW-044 | `host_bash` is denied in worker context
**What happens.** Worker attempts return "ask requires interactive approval; not available in worker context". In one workspace workers could run no shell commands at all.
**Evidence.** `personal/worker_notes.md` 2026-09-28/29 §5; `personal/lessons_learned.md` — `## Gen 64 — problems hit while working live (for the systems engineers)` P4.
**Recurrence.** 4+.
**Impact.** Every shell-based plan in a brief is silently unexecutable.
**Suggested fix.** Publish the worker's actual permission envelope per workspace so briefs can be written against it.

### AW-045 | `CheckSystem` is not in the worker toolset — bans can only be discovered by failing
**What happens.** Workers cannot self-inspect their permissions. The only way to learn that `shell` or `filesystem` is banned is to attempt it and read the refusal, which costs a turn each time.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-13 (b) — fix/session-provider-default`, `## 2026-09-08 — RECENT WORKER-USAGE INCIDENTS REVIEW`, `## 2026-09-09 — … Dispatch #5 …` §F.
**Recurrence.** 4+.
**Impact.** Trial-and-error against the permission system instead of planning.
**Suggested fix.** Add `CheckSystem` (read-only) to the worker toolset.

### AW-046 | Workers have no `Worker` tool, so worker↔worker isolation is untestable; agents cannot remove their own persistent containers
**What happens.** The isolation claims about worker-vs-worker state cannot be tested from inside a worker, because there is no way to spawn or query one. Symmetrically, an agent cannot remove a persistent container it created: "Persistent containers can only be removed by the operator".
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 — live tooling problems … Phase 7 forensics` §5/§6.
**Recurrence.** 2.
**Impact.** Architectural claims rest on unverified assumption; leaked containers accumulate with no agent-side remedy.
**Suggested fix.** Provide a read-only worker list, and an operator-approved removal path initiated by an agent.

### AW-047 | Stale duplicate/spoof trees shadow the real files
**What happens.** A baseline copy at `.thoughtmachine/tmp/r13-5b-baseline/docker_executor.py` and a gitignored `temp/baseline/` both contained older copies. A worker read the old copy and reported the guard as missing; another searched the duplicate and reported the current file as unchanged.
**Evidence.** `personal/worker_notes.md` 2026-10-11 §7; `personal/agent_worker_timeline.md` — `## 2026-09-13 (b) — fix/session-provider-default`.
**Recurrence.** 3.
**Impact.** Findings about code that is not the code under review — the most expensive kind of wrong.
**Suggested fix.** Delete baselines from inside the repo; if kept, require absolute canonical paths in the brief and forbid globbing for implementation files.


---

## Theme F — The agent's own git surface

### AW-048 | `git_read log` silently ignores `ref` and `commit`
**What happens.** Asking for the log of a specific ref or commit returns the log of `HEAD`. No error, no warning — a plausible log for the wrong object.
**Evidence.** `personal/live_harness_findings.md` §2; `personal/worker_notes.md` 2026-10-01.
**Recurrence.** 2.
**Impact.** History-based conclusions drawn about the wrong commit; silent and hard to notice.
**Suggested fix.** Honour `ref`, or reject it; always echo the resolved object in the result.

### AW-049 | `git_read show_file` ignores `line_start`/`line_end` and truncates at ~10k tokens
**What happens.** A requested line range is ignored; the whole file is returned from line 1 and truncated at roughly 10k tokens. On a large file the lines you asked for are simply not in the answer.
**Evidence.** `personal/live_harness_findings.md` §7; `personal/lessons_learned.md` — `## Gen 60 — live tooling problems …` §2/§4; `## Live tooling problems — orchestrator (Gen 60, phases 8-9)` §7.
**Recurrence.** 6+.
**Impact.** The single most expensive read defect in the orchestrator surface; drives repeated blind re-reads.
**Suggested fix.** Implement the range, or report the truncation explicitly with the last line delivered.

### AW-050 | `git_read show_file` cannot read untracked files
**What happens.** Reading a file that exists on disk but is not in `HEAD` returns `fatal: ... exists on disk, but not in 'HEAD'`. The orchestrator has no other way to read arbitrary files.
**Evidence.** `personal/live_harness_findings.md` §7; `personal/lessons_learned.md` — `## Gen 60 …` §8 and §"Corollary".
**Recurrence.** 4+.
**Impact.** New files (the ones under review) are the ones that cannot be inspected.
**Suggested fix.** Fall back to a filesystem read for untracked paths.

### AW-051 | `git_read diff` with a LIST `file_path` returns empty, silently
**What happens.** `file_path=["a.py","b.py"]` yields an empty diff; the same paths as a single string works. There is no "invalid argument" error.
**Evidence.** `personal/worker_notes.md` 2026-09-13 and 2026-09-29; `project/lessons_learned.md` — `## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)` §15.
**Recurrence.** 4.
**Impact.** "No changes" conclusions on files that did change — a false-clean review.
**Suggested fix.** Accept lists, or reject them loudly.

### AW-052 | `git_read show` cannot read file contents, and `format` is fed to `--pretty`
**What happens.** `show` returns only the commit header — never the file body. Separately, `format` is passed to `--pretty`, so `format="--stat"` errors out.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-13 (g) …` TOOLING FACTS 1; `personal/worker_notes.md` 2026-09-30 §4.
**Recurrence.** 3.
**Impact.** Two separate wasted calls; `show` is not usable for the purpose its name suggests.
**Suggested fix.** Document the actual semantics; route `format` to the right flag or refuse non-format strings.

### AW-053 | `git_read cat_file` ignores line ranges
**What happens.** Line range parameters are accepted and then ignored; the file is dumped from the top.
**Evidence.** `project/lessons_learned.md` — `## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)` §14.
**Recurrence.** 2.
**Impact.** Second copy of the AW-049 mechanism on a different verb — same blindness.
**Suggested fix.** Same as AW-049; share one implementation.

### AW-054 | `ls_tree path=<dir>` returns only the directory entry; `merge_base` takes `a`/`b`, not `commit1`/`commit2`
**What happens.** Listing a directory with `path` yields the single tree entry, not its children — the opposite of every `ls` intuition. And `merge_base` silently differs from every other operation in its parameter names.
**Evidence.** `personal/worker_notes.md` 2026-10-01; `project/lessons_learned.md` — Gen 61 §16.
**Recurrence.** 2 each.
**Impact.** Two more wasted calls; the tool surface is not internally consistent.
**Suggested fix.** Non-recursive `path` should list children; normalise parameter names across operations.

### AW-055 | No `fetch` verb anywhere; `origin/dev` is permanently stale; `push` is blocked
**What happens.** Nothing in the surface contacts the remote. `origin/dev` therefore renders identically whether it is 1 commit or 100 commits behind. `git_write push` fails under `network:banned`.
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 — instrumented-load work…` §"Tooling friction observed live"; `## Live tooling problems — orchestrator (Gen 60, phases 8-9)` §"Ask to engineers".
**Recurrence.** 5+.
**Impact.** Any "is this merged / is this upstream?" question is unanswerable, and every answer given is a guess.
**Suggested fix.** An explicit `fetch`, or a result flag stating the ref's age in commits.

### AW-056 | No merge / rebase / amend / reset / cherry-pick / `--no-verify`
**What happens.** The write enum is `commit, init, clone, branch_create, checkout, stage, unstage`. There is no way to move a commit off the wrong branch, amend a message, resolve a conflict, or undo a bad commit.
**Evidence.** `personal/live_harness_findings.md` §4; `personal/worker_protocol.md`; `personal/lessons_learned.md` — `## Gen 64 — problems hit while working live (for the systems engineers)` P8.
**Recurrence.** 6+.
**Impact.** Any of these situations escalates to the operator — a full stop, not a workaround.
**Suggested fix.** Add at least `reset --soft` and `rebase` (or a "move commit" primitive) behind the existing write permission.

### AW-057 | `git_write checkout` is unverifiable from its own output
**What happens.** Checkout printed a plausible success message while `HEAD` stayed put; on another occasion it printed nothing at all. Only `git_read rev_parse ref=HEAD` shows the truth, and `rev_parse abbrev=true` returns a branch *name*, not a SHA.
**Evidence.** `project/lessons_learned.md` — `## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)` §2.
**Recurrence.** 3.
**Impact.** The whole session can proceed on a branch the agent believes it left.
**Suggested fix.** Return the resulting `HEAD` SHA in the checkout result; make `abbrev` semantics explicit.

### AW-058 | A branch was silently reset to `origin/dev` mid-session, destroying an unpushed commit
**What happens.** Mid-session, with no agent action, the working branch returned to `origin/dev`; the unpushed commit was gone. Nothing in any tool result reported the reset.
**Evidence.** `personal/lessons_learned.md` — `## LIVE TOOLING PROBLEM: a branch was silently reset to origin/dev mid-session, destroying an unpushed commit (gen62)` (2026-11-11).
**Recurrence.** 1 recorded, highest severity in the git family.
**Impact.** Irrecoverable work loss with no event to point at.
**Suggested fix.** Never auto-reset a branch; if a reset is ever performed, emit a prominent event. Agents should assert `rev_parse HEAD` before and after every mutating step.

### AW-059 | A merged-and-deleted branch is indistinguishable from data loss
**What happens.** Asking about a branch that was merged and then deleted yields `fatal: ambiguous argument` — exactly the message a lost branch produces.
**Evidence.** `project/lessons_learned.md` — `## Gen 61 — tooling problems hit live, C1/C2 dispatch (2026-11)` §3.
**Recurrence.** 2.
**Impact.** Feeds the false "work is lost" panic mode (see AW-024).
**Suggested fix.** Distinguish "no such ref" from "ref existed and was deleted", and check merged-ness before reporting.

### AW-060 | `git_write worktree_remove` refuses ten orphaned worktrees (~232 MB) and the agent has no `rm`
**What happens.** Ten worktrees pointing at an old clone path cannot be removed: the directory no longer exists, but the removal path refuses, and there is no way to delete the leftover directories or prune the metadata.
**Evidence.** `personal/live_harness_findings.md` §3.
**Recurrence.** 1 standing blocker, permanent.
**Impact.** Permanent disk leak and a permanently blocked cleanup task.
**Suggested fix.** Add `worktree prune`, and permit a force remove when the target directory is absent.

### AW-061 | Path validation is asymmetric between `git_write` and shell commands
**What happens.** `git_write` validates paths against the **host** root (`/home/jojo/PycharmProjects/ThoughtMachine-dev`) and rejects `/workspace/...`; shell commands must use `/workspace/...`. The same logical file has two different, mutually exclusive names depending on which tool you use.
**Evidence.** `personal/live_harness_findings.md` §6; `personal/worker_protocol.md`.
**Recurrence.** 4+.
**Impact.** Every brief has to carry a path-translation table; getting it wrong is a silent rejection or a wrong-file write.
**Suggested fix.** Accept both spellings, or expose one canonical path per file.

### AW-062 | Reads render the checked-out branch, not the branch you mean
**What happens.** `git_read` operations render the working tree as checked out. A `git checkout dev` in an operator script therefore made the dev-branch guards appear to be missing from a file that has them — producing a confident false-positive review finding ("the guards were never committed").
**Evidence.** `project/lessons_learned.md` — Gen 60 §1; `personal/lessons_learned.md` — `## Live tooling problems — orchestrator (Gen 60, phases 8-9)` §2.
**Recurrence.** 3.
**Impact.** False findings about code that is fine; the inverse (missing a real defect) is equally possible.
**Suggested fix.** Always write "read at `<sha>`/`<branch>`" in any report derived from a file read, and verify the branch first.

### AW-063 | Every git result carries `_with_mode` trailer lines that break line-counting
**What happens.** Each result ends with `execution_mode:` and `failure_reason:` lines. Any test asserting on `wc -l` of a git output is off by one or two.
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 Phase 8 — further live tooling problems (confirmed)` §3.
**Recurrence.** 3.
**Impact.** Self-inflicted test failures and wrong "the tool returned N lines" claims.
**Suggested fix.** Put the trailer in a separate structured field, or document a stable separator.

### AW-064 | `git_read status` returns an empty body on a clean tree
**What happens.** A clean tree produces an empty result — indistinguishable from a failed call. Callers that treat empty as "error" retry, and callers that treat empty as "no output" misreport.
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 Phase 8 …` §3 and `## Live tooling problems — orchestrator (Gen 60, phases 8-9)` §9.
**Recurrence.** 3.
**Impact.** Ambiguity on the most frequently asked question in the workflow.
**Suggested fix.** Return an explicit `clean` marker.

### AW-065 | A force-tracked, git-ignored KB file was deleted by a branch switch
**What happens.** `.gitignore` ignores `knowledge/personal/*` and re-includes only `handoffs.md`, but a `git add -f` (c865e83b) had tracked a personal KB file. A subsequent checkout removed it, and `git status` afterwards was clean — no signal at all.
**Evidence.** `personal/live_harness_findings.md` §1.
**Recurrence.** 1 recorded, permanent knowledge loss.
**Impact.** Knowledge-base content destroyed invisibly; the ignore rules and the index disagree.
**Suggested fix.** Never `add -f` inside ignored KB paths; ensure every file you intend to keep has a `!` re-include line.

### AW-066 | New KB files are invisible to git without a `!` negation — "declared done and never written"
**What happens.** A KB entry was reported as written and committed; the file was ignored by the `knowledge/personal/*` rule, so nothing was ever tracked and the claim was unfalsifiable from `git status`.
**Evidence.** `project/lessons_learned.md` — Gen 60 §5.
**Recurrence.** 3.
**Impact.** Whole categories of durable output silently never persisted (see also AW-042).
**Suggested fix.** Verify KB writes with an explicit read-back and `check-ignore` on the path.

### AW-067 | `commit` with `file_path`: untracked files must be `stage`d first, and pathspec commit fails on gitignored-but-tracked paths
**What happens.** Committing a modified tracked file by path works; committing a new untracked file by path fails until `stage` is called. Separately, a pathspec commit on a track-but-ignored path fails outright.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-13 (j) …` lesson 4, `## 2026-08-16 …`.
**Recurrence.** 3.
**Impact.** "Committed" claims that did not commit; a silently empty commit range.
**Suggested fix.** Auto-stage untracked paths given to `commit`, or return an explicit "nothing committed" error.

---

## Theme G — Knowledge base, search, file tools and forensics

### AW-068 | KB `search` is a token bomb
**What happens.** A one-word query (`query="worker"`) returned ~352,940 tokens, truncated at 10k. The tool appears to concatenate whole files rather than return matches.
**Evidence.** `personal/worker_notes.md` 2026-10-11 §5.
**Recurrence.** 3.
**Impact.** One call can consume an entire worker's context budget; the KB becomes unsearchable exactly when it is largest.
**Suggested fix.** Return match lines with context, never whole documents; cap output hard.

### AW-069 | KB files are too large to use, and `append` re-reads and rewrites the whole file
**What happens.** `bugs_and_fixes.md` is 277,081 B (~69k tokens) and `lessons_learned.md` 111,434 B (~28k). `KnowledgeBaseTool.append` reads, modifies and rewrites the entire file, so a single append risks ~100k tokens of context.
**Evidence.** `personal/live_harness_findings.md` §8.
**Recurrence.** 4+.
**Impact.** The KB cannot be appended to by an agent whose context is mostly full — the moment you most want to record something.
**Suggested fix.** Append-only write path (no read), plus per-file rotation.

### AW-070 | KB `mode=summary` crashed
**What happens.** `mode=summary` on `worker_experience_report` raised "`list.append() takes exactly one argument (4 given)`" — a plain implementation bug in the summariser.
**Evidence.** `personal/worker_notes.md` 2026-09-13 and 2026-09-09 C5; `personal/agent_worker_timeline.md` — `## 2026-09-09 …` §C5.
**Recurrence.** 2.
**Impact.** The one mode designed to avoid the size problem is unusable.
**Suggested fix.** Fix the call signature; add a smoke test per mode.

### AW-071 | KB `mode=update` inserts/duplicates section bodies instead of replacing them; there is no prepend mode
**What happens.** Updating a section appended a second copy of the body under the existing heading instead of replacing it, leaving duplicated content. There is also no way to prepend.
**Evidence.** `personal/worker_notes.md` 2026-10-04.
**Recurrence.** 2.
**Impact.** KB files grow with contradictory duplicates; the "current" version is ambiguous.
**Suggested fix.** True replace semantics; add prepend.

### AW-072 | KB byte/line counts disagree with `wc`
**What happens.** The KB reported 568271 B / 1759 lines where `wc` reported 572643 B / 1758 lines. Any conclusion based on KB-reported size is unreliable.
**Evidence.** `personal/worker_notes.md` 2026-09-28/29 §6.
**Recurrence.** 2.
**Impact.** Budgeting and truncation decisions made on wrong numbers.
**Suggested fix.** Re-measure with `wc -c` / `wc -l` / `sha256sum`; fix the internal counter.

### AW-073 | KB domain resolution is surprising: `lessons_learned` resolves to `personal/`, not `project/`; slashes are rejected
**What happens.** `domain=lessons_learned` serves `personal/lessons_learned.md` even when the intent is the project one. Domain names must be a bare identifier — any slash yields "Unknown domain", so a path cannot be given.
**Evidence.** `project/lessons_learned.md` — `## Gen 61 …` §5; `personal/worker_notes.md` 2026-10-12 (D1/D2).
**Recurrence.** 3.
**Impact.** Workers read the wrong lessons file and re-learn the wrong lessons.
**Suggested fix.** Qualify domains (`personal/lessons_learned`, `project/lessons_learned`) and accept paths.

### AW-074 | KB argument strictness: `append` requires `entry`, `mode` is mandatory, `create_domain` needs `category` to be exactly `project`|`personal`
**What happens.** Three separate signature surprises: the text must arrive under `entry` (not `content`/`text`), `mode` must be supplied explicitly even when obvious, and `create_domain` rejects anything but the two literal category strings.
**Evidence.** `personal/live_harness_findings.md` §9.
**Recurrence.** 4+.
**Impact.** One wasted call per first use of each mode.
**Suggested fix.** Document the exact signatures; accept the obvious aliases.

### AW-075 | `FileSearchTool` is unreliable in five distinct ways
**What happens.** (1) Searching `.` fails with "42033 files". (2) With `show_line_numbers=false, max_results=0` it returns a **spurious** "No matches found" — a false negative that looks authoritative. (3) It under-reports: 1 match for a heading that occurs 13×. (4) Synthetic bold markers inserted into results break exact-match edits. (5) The `filenames` parameter is unreliable.
**Evidence.** `personal/lessons_learned.md` — Gen 61 §11/§13 and `## Gen 60 Phase 8 …` §2; `personal/agent_worker_timeline.md` — `## 2026-09-13 — fix/start-drift-mutation plan-first dispatch — lessons` §3; `personal/worker_notes.md` 2026-09-02.
**Recurrence.** 6+ across four months.
**Impact.** Search is the entry point of nearly every task; a silent false negative is the most damaging failure mode in the whole toolset after AW-001.
**Suggested fix.** Never return a bare "no matches" without the parameters used; default to a bounded directory; never mutate snippets; honour `filenames`.

### AW-076 | `ReadFile` ignores line ranges on large files; `FilePreviewTool` range parsing flakes
**What happens.** On large files `ReadFile` returns from the top regardless of the requested range. `FilePreviewTool` accepts a range but intermittently returns the wrong one.
**Evidence.** `personal/worker_notes.md` 2026-10-12 (D3) and 2026-10-14 (adversarial verification).
**Recurrence.** 5+.
**Impact.** Citation work on big files — exactly where accuracy matters — silently cites the wrong lines.
**Suggested fix.** Return the range or an explicit error; echo the range actually delivered.

### AW-077 | `WorkingDocument create` ignores a supplied `doc_id`; `append` injects a timestamp prefix; working docs are gitignored
**What happens.** A named `doc_id` is not honoured, so a predictable id cannot be relied on. Every append gains a `[YYYY-MM-DD HH:MM:SS]` prefix that must be stripped by any consumer. And the store is gitignored, so it is not a durable deliverable.
**Evidence.** `personal/lessons_learned.md` — `## Gen 60 — instrumented-load work…` §"Tooling friction observed live" and `## Live tooling problems — orchestrator (Gen 60, phases 8-9)` §11/§12.
**Recurrence.** 4+.
**Impact.** Hand-off documents that look canonical are ephemeral, uncommitted and unaddressable by name.
**Suggested fix.** Honour `doc_id`; make the timestamp opt-in; document durability honestly.

### AW-078 | `FileEditor` under-reports multi-line appends
**What happens.** An append of many lines reports "1 line(s) added". The content is written; the report is wrong.
**Evidence.** `project/lessons_learned.md` — `## Gen 61 …` §12; `personal/lessons_learned.md` — `## Gen61 — tooling/agent-worker problems seen live (reported to the engineer)` §5.
**Recurrence.** 4+.
**Impact.** Verification of "did the write land?" is defeated by a wrong counter, inviting duplicate appends.
**Suggested fix.** Report an accurate added/deleted line count, or none at all.

### AW-079 | No per-job worker forensics; blocking spawns never appear in `job_status`
**What happens.** `job_status` tracks only `submit_query` jobs. Every blocking `spawn`/`query` — i.e. virtually all real usage — is unrecorded, so "what happened to worker X?" cannot be answered after the fact. (Cross-reference AW-017.)
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-09 — … Dispatch #5 …` §A/§F.
**Recurrence.** 3.
**Impact.** Incidents cannot be reconstructed; every post-mortem in the corpus is anecdotal.
**Suggested fix.** Record every dispatch: worker name, elapsed, calls, tokens, outcome, payload-lost flag.

### AW-080 | The orchestrator has no shell and no workspace file-read tool
**What happens.** The orchestrator's entire surface is `Worker`, `git_read`/`git_write`, `KnowledgeBaseTool`, `WorkingDocument`, `CheckSystem`, `Respond`, `SummarizeTool`. There is no `Bash`, and no generic file reader. Every observation of the workspace is second-hand, via git verbs (with the defects in Theme F) or via a worker.
**Evidence.** `personal/lessons_learned.md` — `## Live tooling problems — orchestrator (Gen 60, phases 8-9)` §1; `## Gen 64 — problems hit while working live (for the systems engineers)` P2.
**Recurrence.** Structurally permanent.
**Impact.** The orchestrator cannot independently verify anything it is told, which is the root enabler of Themes D and F — and the reason every dispatch needs a second verification pass.
**Suggested fix.** Give the orchestrator at least a read-only file reader (with line ranges) and a bounded `grep`.

### AW-081 | `FileEditor delete` leaves a 0-byte file behind
**What happens.** Deleting a file whose content is empty leaves a zero-byte file on disk rather than removing it.
**Evidence.** `personal/agent_worker_timeline.md` — `## 2026-09-06 — WORKER LAYER-2 LOCKDOWN RESOLVED`.
**Recurrence.** 2.
**Impact.** Cleanup claims are wrong; empty files show up as untracked additions.
**Suggested fix.** Unlink the path after emptying it.

---

## Summary table

| ID | Theme | Title | Recurrence | Severity |
|----|-------|-------|-----------|----------|
| AW-001 | A | Long fresh-spawn `query` payloads silently destroyed | 4+ | Critical |
| AW-002 | A | `context["meta"]` dropped on spawn | 3 | High |
| AW-003 | A | `context["briefing"]` dropped in transit | 3 | High |
| AW-004 | A | `spawn` on running instance no-ops | 2 | High |
| AW-005 | A | `worker_name` required despite docs | 6+ | Medium |
| AW-006 | A | `query` requires `worker_query`, not `context` | 3 | Medium |
| AW-007 | A | `force=true` kills cross-session instances | 2 | High |
| AW-008 | A | Workers rewrite the verbatim brief | 5+ | Critical |
| AW-009 | A | "Worker already has this" premise is false | 2 | High |
| AW-010 | A | Workers silently skip a mandated phase | 2 | High |
| AW-011 | B | Turn cap drifts; `timeout_seconds` not a bound | 8+ | Critical |
| AW-012 | B | Worker finished, killed before `Respond` | 3 | Critical |
| AW-013 | B | Envelope `status`/`confidence` untrustworthy | 10+ | Critical |
| AW-014 | B | No soft-timeout / restricted-tools flag | 5+ | High |
| AW-015 | B | Envelope shape drifts between calls | 3 | Medium |
| AW-016 | B | Instance degrades after ~200 tool calls | 2 | High |
| AW-017 | B | Background work cannot survive the harness | 3 | High |
| AW-018 | C | Token-ceiling revocation lands mid-edit | 4+ | High |
| AW-019 | C | Ceilings soft and per-worker | 3 | Medium |
| AW-020 | C | Lost attribution of own edits after summarisation | 2 | Medium |
| AW-021 | C | Context-burnt worker not reusable | 4 | Medium |
| AW-022 | D | Workers confirm what they are asked to confirm | 5+ | Critical |
| AW-023 | D | Workers misreport git facts / stale SHAs | 3 | High |
| AW-024 | D | "Data lost" false alarms | 2 | High |
| AW-025 | D | Provenance confabulation | 3 | High |
| AW-026 | D | Unchecked facts asserted at high confidence | 4+ | High |
| AW-027 | D | Self-reported test counts untrustworthy | 4 | High |
| AW-028 | D | Regressions induced by weakening tests | 3 | Critical |
| AW-029 | D | Confident wrong deliverable; `needs_direction` as confession | 3 | High |
| AW-030 | D | Acceptance gate must be mutation, not green | 3 | High |
| AW-031 | D | Whole-file rewrite clobbers a module | 2 | Critical |
| AW-032 | D | Misleading shell exit-code reporting | 3 | Medium |
| AW-033 | D | Worker cannot tell which branch/worktree you meant | 2 | High |
| AW-034 | E | No git binary in worker shell; `git_*` tools do work | 8+ | High |
| AW-035 | E | `/tmp` not persisted across runner calls | 4+ | High |
| AW-036 | E | 64 MB `/tmp` exhaustion ⇒ spurious failures | 2 | High |
| AW-037 | E | `DockerCodeRunner` `image` ignored / not reusable | 3 | Medium |
| AW-038 | E | No node/npm; `/var/tmp` read-only ⇒ misleading `ENOENT` | 3 | High |
| AW-039 | E | Broken `.venv/bin/python` symlink | 2 | Low |
| AW-040 | E | Relative paths resolve against `/workspace`, not the worktree | 3 | High |
| AW-041 | E | `rm` glob hygiene: `/workspace/tmp/` is the repo `tmp/` | 2 | Medium |
| AW-042 | E | `Respond(report_body=)` writes into gitignored `reports/` | 2 | Medium |
| AW-043 | E | Poisoned worker session uncleared by `force`/restart | 2 | Critical |
| AW-044 | E | `host_bash` denied in worker context | 4+ | High |
| AW-045 | E | `CheckSystem` missing from worker toolset | 4+ | Medium |
| AW-046 | E | No `Worker` tool in worker; can't remove own containers | 2 | Medium |
| AW-047 | E | Stale duplicate/spoof trees shadow real files | 3 | High |
| AW-048 | F | `git_read log` ignores `ref`/`commit` | 2 | High |
| AW-049 | F | `show_file` ignores line ranges; truncates at ~10k tokens | 6+ | Critical |
| AW-050 | F | `show_file` cannot read untracked files | 4+ | High |
| AW-051 | F | `diff` with LIST `file_path` returns empty silently | 4 | Critical |
| AW-052 | F | `show` cannot read contents; `format`→`--pretty` | 3 | Medium |
| AW-053 | F | `cat_file` ignores line ranges | 2 | High |
| AW-054 | F | `ls_tree path` returns only the dir; `merge_base` uses `a`/`b` | 2 | Medium |
| AW-055 | F | No `fetch`; `origin/dev` permanently stale; `push` blocked | 5+ | High |
| AW-056 | F | No merge/rebase/amend/reset/cherry-pick | 6+ | High |
| AW-057 | F | `checkout` unverifiable from its own output | 3 | High |
| AW-058 | F | Branch silently reset to `origin/dev`; commit destroyed | 1 | Critical |
| AW-059 | F | Merged-and-deleted branch looks like data loss | 2 | Medium |
| AW-060 | F | `worktree_remove` refuses 10 orphaned worktrees; no `rm` | 1 standing | Medium |
| AW-061 | F | Path validation asymmetric: host root vs `/workspace` | 4+ | High |
| AW-062 | F | Reads render the checked-out branch, not the one you mean | 3 | Critical |
| AW-063 | F | `_with_mode` trailer breaks line-counting | 3 | Low |
| AW-064 | F | `status` returns an empty body on a clean tree | 3 | Low |
| AW-065 | F | Force-tracked git-ignored KB file deleted by checkout | 1 | High |
| AW-066 | F | New KB files invisible to git without `!` negation | 3 | High |
| AW-067 | F | Pathspec `commit` needs `stage` first for untracked files | 3 | Medium |
| AW-068 | G | KB `search` is a ~353k-token bomb | 3 | Critical |
| AW-069 | G | KB files too large; `append` rewrites the whole file | 4+ | High |
| AW-070 | G | KB `mode=summary` crashes | 2 | Medium |
| AW-071 | G | KB `mode=update` duplicates sections; no prepend | 2 | Medium |
| AW-072 | G | KB byte/line counts disagree with `wc` | 2 | Low |
| AW-073 | G | `domain=lessons_learned` resolves to `personal/`; no slashes | 3 | High |
| AW-074 | G | KB argument strictness (`entry`, `mode`, `category`) | 4+ | Low |
| AW-075 | G | `FileSearchTool` unreliable in five distinct ways | 6+ | Critical |
| AW-076 | G | `ReadFile` ignores ranges; `FilePreviewTool` range flakes | 5+ | High |
| AW-077 | G | `WorkingDocument`: `doc_id` ignored, timestamp prefix, gitignored | 4+ | High |
| AW-078 | G | `FileEditor` under-reports multi-line appends | 4+ | Low |
| AW-079 | G | No per-job worker forensics; blocking spawns unrecorded | 3 | High |
| AW-080 | G | Orchestrator has no shell and no file reader | permanent | Critical |
| AW-081 | G | `FileEditor delete` leaves a 0-byte file | 2 | Low |

**Totals:** 81 items — A 10, B 7, C 4, D 12, E 14, F 20, G 14.

---

## Provenance and limitations of this harvest

**Files read in full:** `personal/worker_experience_report.md` (a 662 B pointer stub), `personal/worker_protocol.md`, `personal/live_harness_findings.md`, `personal/worker_notes.md` (317 lines), `personal/agent_worker_timeline.md` (776 lines), `project/lessons_learned.md`, `project/KB_MAP.md`, `project/structural_debt_register.md`.

**Files read in part:** `personal/lessons_learned.md` (lines 1–282 and 523–572, plus a full heading index).

**Files NOT read (context/time budget exhausted):** `personal/lessons_learned_archive_2026-10-29.md` (2671 lines), `personal/lessons_learned_archive_2026-11-05.md`, `personal/task_tracker.md`, `personal/bugs_and_fixes.md`, `project/open_items_register.md`, `project/working_agreements.md`, `project/status.md`.

Consequently this list is **not exhaustive**. The two archive files are the likeliest source of additional distinct mechanisms, and `bugs_and_fixes.md` (277 kB) is the likeliest source of additional tool defects. Every item here is grounded in a quote or a specific heading in a file that was actually read; nothing is extrapolated.

