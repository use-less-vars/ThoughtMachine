# Part 2 — Agent/Worker Problem Harvest from Working Documents

**Source:** `.thoughtmachine/working_docs/` — 340 `*.json` working documents enumerated (plus loose `.md`/`.log`/`.py` siblings, not counted here).
**Read:** 24 documents in full (every section quoted below is from those 24).
**Skipped as clean:** 7 read documents produced no defect finding (listed at the end of this preamble).
**Not read:** 316 documents — budget-limited; see *Limitations*.
**Scope:** behavioural problems in the agent/worker/orchestrator system as recorded by the agents that hit them; this part is a harvest, not new investigation. No code was changed; the only file written is this report.

No-defect documents (read, nothing to harvest): `2a4a6f7c8254`, `3977cbf978ab`, `3d378c8b3d74`, `a87fc3807229`, `58edeb4a1bfd`, `f3d4b6e56f58`, `aa9cba808d95`.

---

## Theme 1 — Dispatch & briefings

### AW-101 — Non-`query` context keys are silently dropped in transit
**What happens.** The orchestrator may pass a full brief as `briefing`, `docs`, `purpose`, `style`, `meta` in the `Worker` tool's `context` dict, but only `query` survives the hop to the worker. The others are discarded somewhere between the tool call and `Worker.execute`, with no warning to either party.
**Evidence.** "Worker Control: How We Got Here" (`cffb1671448a`), §04 *Dispatch & briefing*.
**Recurrence.** 6 — 2026-08-02, 2026-08-05, 2026-08-29, 2026-11-03, plus class-B dispatch notes and A6.
**Impact.** blocks work.
**Suggested fix.** Versioned brief carrier with delivery ack plus a mandatory restatement point inside the worker's first turn.

### AW-102 — Real task in `briefing`, stub in `query` → wrong file edited
**What happens.** Because only `query` arrives, an orchestrator that put the actual instruction in `briefing` and a pointer ("see briefing") in `query` leaves the worker with no task. On 2026-11-09 a worker in exactly this configuration edited the *wrong* test file and reported success.
**Evidence.** `cffb1671448a` §04.3 (2026-11-09); also "Worker Lifecycle Refactor Tracker" (`65b5b92028cd`) W2, where the missing briefing produced off-spec test names.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Persist the brief to disk *and* inline it in `query` — the 2026-09-19 agreement that was never enforced.

### AW-103 — Brief eaten by worker summarisation mid-task
**What happens.** A worker that summarises its own context mid-run can lose the brief entirely. A 15-item recon brief was summarised away and the worker's output silently omitted items 1–8. Nothing in the protocol re-asserts the brief after a summary event.
**Evidence.** `cffb1671448a` §04.4(ii).
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** Nothing re-asserts it today — add a restatement point after every `context_summarized`.

### AW-104 — Briefs have no delivery receipt and no ack
**What happens.** The author of a brief cannot learn whether it arrived, was truncated, or was dropped. Failures therefore surface only as wrong output minutes or hours later, and are frequently misattributed to worker incompetence.
**Evidence.** `cffb1671448a` §04.4.
**Recurrence.** 3.
**Impact.** causes wrong output.
**Suggested fix.** Add an ack (and a brief hash) to the spawn/query envelope.

### AW-105 — `reset()` destroys the brief; re-context stacks duplicates; nested `meta` dropped
**What happens.** `reset()` clears the conversation including the brief, so post-reset work runs on whatever the new `query` said. Sending `context` again stacks a second copy of the brief in the history instead of replacing the first. Nested dicts under `meta` are dropped (recorded at Generation 60).
**Evidence.** `cffb1671448a` §4.1 (Gen 60).
**Recurrence.** 3.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-106 — `spawn(force=True)` returns `spawned:false, status:"busy"` with no error
**What happens.** A forced spawn against a busy worker returns a success-shaped envelope with `spawned: false` and `status: "busy"`. The orchestrator's next `query` then lands on the *old* instance and answers the *previous* task. Documented as a ticket on 2026-11-04.
**Evidence.** `cffb1671448a` §03 Class B.
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** Reject loudly on busy (non-2xx-shaped envelope) instead of returning a soft flag.

### AW-107 — Workers can never ask
**What happens.** Worker tool calls that need approval go through `NullEventBus.ask()`, which returns deny immediately. There is no path by which a worker can request a decision, so any "ask" grain degrades to "deny" for all worker work.
**Evidence.** `cffb1671448a` §05.1.
**Recurrence.** 3.
**Impact.** blocks work.
**Suggested fix.** none recorded.

### AW-108 — Worker `host_bash` "ask" prompts the OPERATOR instead of auto-denying
**What happens.** In worker context a `host_bash` call whose grain is `ask` calls `_request_approval`, which raises a real approval prompt to the human operator with a 120s timeout. The worker is supposed to be isolated from the operator's attention; instead it can block on them.
**Evidence.** "Worker ask-policy task progress" (`0c0726bcc201`), findings (2026-09-08).
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Inject `is_worker_context` into `ToolBase` so ask resolves to auto-deny in workers.

### AW-109 — CheckSystem / Worker / KB tools unavailable to workers
**What happens.** `_WORKER_BLOCKLIST` removes Worker-control and CheckSystem from a worker's toolset, and knowledge-base access is likewise unavailable, so a worker cannot inspect the vault or its own runtime and must reconstruct facts by reading source.
**Evidence.** "Token Ceiling Probe" (`b49221273ec7`); "Worker Lifecycle Audit (read-only)" (`719e62e85d56`) chunk A; "worker_cap_reset_continue_impl" (`9d5b6d43326f`).
**Recurrence.** 3.
**Impact.** costs significant time.
**Suggested fix.** none recorded.

### AW-110 — Orchestrator sends workers to nonexistent paths
**What happens.** Briefs have instructed workers to read `personal/parking_lot.md` and to write into `personal/`; neither exists at repo root. Workers spend recon budget discovering this and improvising.
**Evidence.** "git_info_tool host-execution gate investigation + parking lot" (`22df570cac07`), part 2 (2026-09-11).
**Recurrence.** 2.
**Impact.** costs significant time.
**Suggested fix.** none recorded.

---

## Theme 2 — Timeout & turn lifecycle

### AW-111 — Four timeout roles are conflated and were never unified
**What happens.** Harness response cap, query budget, spawn-queue wait and per-tool timeout are four separate clocks with overlapping names, and no single authority decides which applies. The unification roadmap item has been open since 2026-08-25.
**Evidence.** `cffb1671448a` §1.1 / §1.4.
**Recurrence.** 4.
**Impact.** blocks work.
**Suggested fix.** One clock, declared in the protocol.

### AW-112 — Soft timeout never detected: lowercase `'critical'` vs uppercase `'CRITICAL'`
**What happens.** Production `TimeState` values are lowercase (`'critical'`) while the check compared against `'CRITICAL'`, so the soft-timeout path never fired in production — for months. Fixed 2026-08-29 (defect D3).
**Evidence.** `cffb1671448a` §1.2 D3.
**Recurrence.** 3.
**Impact.** causes wrong output.
**Suggested fix.** Also check `restriction_reason` plus elapsed time rather than a single string compare (done).

### AW-113 — Requested `timeout_seconds` ignored; spawn onto live instance dropped the query
**What happens.** D1: a caller-supplied `timeout_seconds` was not honoured. D2: spawning onto a live non-paused instance silently discarded the query. Both fixed 2026-08-29 (dispatch 2); `QUERY_WAIT_GRACE_SECONDS=60` added.
**Evidence.** `cffb1671448a` §1.2 (2026-08-29, dispatch 2).
**Recurrence.** 3.
**Impact.** blocks work.
**Suggested fix.** D1/D2 fixed; grace constant added.

### AW-114 — Harness caps the blocking response window independently
**What happens.** Observed blocking waits truncated at ~150s, ~409s and ~540s in different runs, plus a ~480s hard kill that returns nothing at all. The worker is never told the caller went away, so a completed answer is thrown away.
**Evidence.** `cffb1671448a` §1.1.
**Recurrence.** 3.
**Impact.** blocks work.
**Suggested fix.** none recorded — the note is explicitly "configured ≠ observed".

### AW-115 — No thread kill exists anywhere
**What happens.** The commonly cited "600s" is `SPAWN_QUEUE_TIMEOUT` and "300s" is the query cutoff; neither stops a thread. `stop`/`force` pop the registry while the daemon thread keeps running and keeps writing `context.json`, so a "stopped" worker can still mutate state afterwards.
**Evidence.** "Worker Timeout Mechanisms — Audit & Engineer Handoff" (`97d74f03cceb`), hard-timeout section (2026-08-10).
**Recurrence.** 5.
**Impact.** blocks work.
**Suggested fix.** WLM is the only real kill path, and it ships off by default.

### AW-116 — Pause/stop checkpoints never fire during a tool call
**What happens.** `ToolExecutor.execute_tool_calls` runs synchronously and polls neither `_stop_event` nor the pause flag, so a stop only takes effect at the next `turn_start`. Every in-flight tool call — including a multi-minute container exec — always runs to completion.
**Evidence.** "Worker Lifecycle Audit (read-only)" (`719e62e85d56`), chunk B.
**Recurrence.** 3.
**Impact.** blocks work.
**Suggested fix.** none recorded.

### AW-117 — The main agent cannot stop a blocked worker
**What happens.** `Worker.execute()` blocks for up to 300s (query) or 600s (spawn) inside `send_query`, and the same agent cannot issue `action='stop'` while it is blocked in that call. Only an out-of-band `command.json` written by the web API escapes the deadlock.
**Evidence.** `719e62e85d56` chunk B.
**Recurrence.** 3.
**Impact.** blocks work.
**Suggested fix.** Async handle or out-of-band control channel (only the web API's `command.json` works today).

### AW-118 — Worker soft-timed-out at 333.9s against a 300s budget yet delivered a complete answer
**What happens.** In one audit run a worker was soft-timed-out at 333.9s although its budget was 300s, while the answer it eventually produced was complete. The budget is thus neither enforced nor meaningful.
**Evidence.** `719e62e85d56` chunk C.
**Recurrence.** 1.
**Impact.** costs significant time.
**Suggested fix.** none recorded.

### AW-119 — No dedicated stop-propagation test
**What happens.** Stop and join behaviour is covered only indirectly through other tests; there is no test asserting that a stop reaches an in-flight turn or that the thread actually ends.
**Evidence.** `719e62e85d56` chunk B.
**Recurrence.** 1.
**Impact.** causes wrong output.
**Suggested fix.** Add explicit stop-propagation tests.

---

## Theme 3 — Pause / stop / detach / revive

### AW-120 — Detach or stop mid-flight silently loses the in-flight tool result
**What happens.** Killing a worker while a tool call is executing discards that call's result: the turn is not journalled, so on revive the worker has no record that the tool ran, and may re-run a mutating operation or claim it never happened.
**Evidence.** "S7b — Detach Recovery (design pass)" (`76275ee9699f`), brief question 2 (2026-11-04).
**Recurrence.** 3.
**Impact.** blocks work.
**Suggested fix.** Journal `inflight.json` before killing, then kill (Option K in the brief).

### AW-121 — `pause()` discarded mid-turn work
**What happens.** Pausing while a turn was mid-flight dropped that turn's accumulated work; the test-first proof was 2/2 failures, fixed 2026-11-04 by letting the paused turn finish (`_pause_reported`).
**Evidence.** `76275ee9699f`, S7b-0 (commit `68672edb`).
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Let the paused turn finish (implemented).

### AW-122 — Mid-turn pause publishes `worker_paused` and writes `status.json` twice
**What happens.** A mid-turn pause emits `worker_paused` and writes `status.json` from both the loop (:2494) and `run()` (:3233), producing duplicate events and duplicate writes. No test pins the single-emission contract, so it can regress freely.
**Evidence.** `76275ee9699f` S7b-0.
**Recurrence.** 2.
**Impact.** cosmetic.
**Suggested fix.** none recorded — behaviour is unpinned by tests.

### AW-123 — Main/global pause pauses ASYNC workers too
**What happens.** `bridge.pause()` iterates all workers with no sync/async predicate and bypasses `_action_pause`'s `_SESSION_MAIN_PAUSED` scope semantics, so a global pause terminalises async jobs as `paused` even though they were never scoped as part of the main session.
**Evidence.** "worker_instance_ui_build" (`17a5e833f598`) (2026-08-24).
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** Skip workers running `submit_query` jobs.

### AW-124 — Any follow-up query auto-resumes a paused worker
**What happens.** A paused worker resumes on the next `query` with no operator action (`worker.py:3549`), and the UI pause path never sets `_manual_only_pause`, so an "operator-initiated pause" is indistinguishable from an automatic one and does not stick.
**Evidence.** `17a5e833f598`.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Resume only for operator-initiated resumes (W6 ruling).

### AW-125 — Detached instance keeps its `instance_id` slot
**What happens.** A detached instance still owns its `instance_id`, blocking a clean respawn of the same logical worker. The only way forward is `reset`, which drops the accumulated context — the exact thing detaching was meant to preserve.
**Evidence.** `76275ee9699f`, brief.
**Recurrence.** 3.
**Impact.** blocks work.
**Suggested fix.** Journal the instance then kill it, freeing the slot.

### AW-126 — `_WorkerAmbiguityError` on name collisions
**What happens.** When several live instances match a worker name and the caller did not supply `instance_id`, the runtime raises `_WorkerAmbiguityError` rather than picking deterministically, so session-scoped name reuse surfaces as a hard error.
**Evidence.** "worker_cap_reset_continue_impl" (`9d5b6d43326f`), state section.
**Recurrence.** 1.
**Impact.** costs significant time.
**Suggested fix.** none recorded.

---

## Theme 4 — Context & token ceilings

### AW-127 — "Context doubling 68k→136k" was fixed, then disproven
**What happens.** An observed jump from 68k to 136k tokens was diagnosed as context doubling, and F1–F4 were shipped against it. Four instrumented spawns then showed overhead of only +1,987, +2,752 and +454 tokens with no doubling — the real mechanism was the spawn dict, not history duplication. The earlier fix rests on a disproven premise.
**Evidence.** `97d74f03cceb`, repeat-experiment-results (2026-08-10 18:50).
**Recurrence.** 4.
**Impact.** causes wrong output.
**Suggested fix.** none recorded — the mechanism was the spawn dict, not doubling.

### AW-128 — The reported token number is the TOTAL prompt, not the conversation
**What happens.** The figure surfaced to the agent is total prompt tokens (system prompt + tool schemas + conversation), not the conversation alone, so the number exceeds what an agent can account for and gets misread as runaway history.
**Evidence.** "Token Ceiling Probe" (`b49221273ec7`), source_findings (`agent.py:1129-1130`).
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-129 — Warning gate compares exact totals but prints `tokens//1000`
**What happens.** The threshold check uses the exact integer while the human-readable message floors to thousands, so a warning appears at "67k" for a 65,000-token threshold. Every such message reads as a bug report about arithmetic.
**Evidence.** `b49221273ec7`, timeline.
**Recurrence.** 2.
**Impact.** cosmetic.
**Suggested fix.** none recorded.

### AW-130 — The WARNING tier does not restrict tools
**What happens.** A `WorkingDocument` append succeeded while the agent was in the WARNING tier; only CRITICAL restricts the toolset to Respond/SummarizeTool. Callers that branch on "restricted" therefore over-estimate how constrained they are.
**Evidence.** `b49221273ec7`, timeline (2026-09-24).
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-131 — Worker blocked from appending to its own working doc
**What happens.** Because the token restriction removes everything but Respond/SummarizeTool, a worker mid-task could not record its findings; the orchestrator had to act as scribe for it.
**Evidence.** `97d74f03cceb`, status (2026-08-10 01:11).
**Recurrence.** 2.
**Impact.** costs significant time.
**Suggested fix.** none recorded.

### AW-132 — Stop-path saves persisted uncompacted pre-summary history
**What happens.** The stop paths wrote the full pre-summary history to `context.json`; compaction ran only at pause and post-query, so a stopped worker revived with an inflated context.
**Evidence.** `97d74f03cceb`, context-doubling section.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** F3 — compact on stop paths (done).

### AW-133 — No mid-query checkpointing
**What happens.** `context.json` is written only at query-end, pause, stop, thread-end and shutdown. A worker that dies mid-query (crash, container reclaim, kill) loses everything since the last checkpoint; there is a documented counter-strategy of querying the worker periodically purely to force a save.
**Evidence.** `97d74f03cceb`, respawn-query-sync (c).
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** none recorded.

### AW-134 — Tool output capped at 10,000 tokens; worker's own "largest files" heuristic picked unreadable files
**What happens.** Tool output is truncated at 10,000 tokens with a notice the worker must notice for itself ("original was 10016 tokens"). Separately, a worker asked to find the largest files picked a 10.4MB and a 9.1MB `.jsonl` log and could not read them, burning a whole recon run.
**Evidence.** `97d74f03cceb`, PROTOCOL RUN 2; `b49221273ec7`.
**Recurrence.** 2.
**Impact.** costs significant time.
**Suggested fix.** none recorded.

---

## Theme 5 — Envelope & telemetry truthfulness

### AW-135 — `query` on a ready worker returns the STALE previous answer
**What happens.** When a worker holds a stored final response, a new `query` returns that stored answer immediately — with the old elapsed figures, e.g. 133.6s then 29.8s — while queueing the new task. The orchestrator reads a successful envelope for work that has not started. Confirmed twice.
**Evidence.** `97d74f03cceb`, PROTOCOL RUN 2 and RUN 3.
**Recurrence.** 3.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-136 — The "one query behind" desync family
**What happens.** Answers arrive attributed to the wrong query: each response corresponds to the previous dispatch. Reproducibly observed from 2026-08-06 onward, and it survived several independent fixes.
**Evidence.** `cffb1671448a` §03 Class B.
**Recurrence.** 4.
**Impact.** causes wrong output.
**Suggested fix.** Deadlines in the protocol (per-query id echoed and checked).

### AW-137 — Drained query returned under the NEW query id with null metadata
**What happens.** A query queued and then drained by a pause/detach comes back attached to the *new* query id with `status`, `confidence` and `meta` all `null`. Observed live 2026-11-09.
**Evidence.** `cffb1671448a` §2.5.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-138 — stop-mid-query envelope has `status: null`
**What happens.** The envelope for a query stopped mid-flight carries `status: null`; the word "stopped" exists only inside the content JSON, because `_respond_metadata` is populated solely from `agent_responded`, which never fires on this path.
**Evidence.** `719e62e85d56`, chunk B.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-139 — Worker receives `None` for status/confidence/meta from the parent's Respond
**What happens.** The metadata present on the orchestrator's Respond is lost at three separate points between `tool_executor` and `agent_responded`, so a worker that is supposed to mirror its parent's verdict sees three nulls.
**Evidence.** "fix-worker-metadata-null" (`4203ce23ce2d`), problem section (2026-07-27).
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Forward metadata explicitly through `tool_executor` → `agent_responded`.

### AW-140 — `status` has ~17 writers and no owner
**What happens.** `worker_thread.py` assigns `status` from roughly 17 sites with no single owner and no compare-and-set, so the last writer wins and contradictory statuses are observable within one turn.
**Evidence.** `cffb1671448a` §03 Class A.
**Recurrence.** 3.
**Impact.** causes wrong output.
**Suggested fix.** One writer per field.

### AW-141 — `paused` status can survive `kill()`
**What happens.** The park branch at `:2974-2978` has no `_stop_event` guard, and two `command.json` consumers race, so a killed worker can retain `status: paused`. Filed P0 on 2026-11-09.
**Evidence.** `cffb1671448a` §03 Class A.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Kill is a framed command with an ack.

### AW-142 — Restriction written outside the state API
**What happens.** `worker_thread.py:3754-3756` assigns the restriction field directly instead of calling `restrict()`, making the transition invisible to instrumentation. A whole investigation generation was lost looking for a restriction that had been set off-API.
**Evidence.** `cffb1671448a` §03 Class A (Generation 61, dispatch 13).
**Recurrence.** 2.
**Impact.** costs significant time.
**Suggested fix.** Route every write through `restrict()`.

### AW-143 — Two `command.json` readers race
**What happens.** The run-loop park wait (`:3576`) and the poll (`:3586`) both consume the same file; the loser returns silently and writes no status, so a command can be consumed with no observable effect.
**Evidence.** `cffb1671448a` §5.1.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Single consumer (the P2 fence).

### AW-144 — Six event-egress families; hooks cover two
**What happens.** `WorkerThread` publishes events through six distinct egress families. Hooking both helper methods covers 2 of 6, and `EventBus.publish` has no publish hook at all, so any observer built on helper hooks misses most traffic.
**Evidence.** `cffb1671448a` §03 Class C (2026-11-03).
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** Wildcard `subscribe(None, cb)`.

### AW-145 — Per-worker bus lazily created in the thread body
**What happens.** `__init__` sets the per-worker bus to `None`; the real bus is created inside the thread body. A child that arrives early polls every 10ms for up to 5s and then gives up silently, and events are dropped silently for as long as `_worker_bus_adapter` is `None`.
**Evidence.** `cffb1671448a` §03 Class C.
**Recurrence.** 2.
**Impact.** cosmetic.
**Suggested fix.** A bus-created hook instead of polling.

### AW-146 — Work events carry no `instance_id`
**What happens.** Per-worker-bus work events carry only `worker_name` while global lifecycle events carry the full envelope, so a panel filtered by instance drops every work event.
**Evidence.** `17a5e833f598`, live_test_and_bugfix.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Bridge derives the instance from the label.

### AW-147 — `worker_partial_result` unconditionally dropped
**What happens.** The event is published on the global bus but is absent from the bridge's global subscription list, so it is dropped every time; the frontend adapter also had no case for it.
**Evidence.** `17a5e833f598`.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Subscribe to it and add an adapter case.

### AW-148 — Panel empty after F5 until the next `context_summarized`
**What happens.** After a browser refresh, events are dropped at `bridge.py:607-612` because no WS callback is registered yet, and nothing buffers or replays them. The panel stays empty — up to a whole summarisation interval — and the gap events are lost permanently.
**Evidence.** "worker_panel_post_refresh_trace" (`240636202e57`), root_cause.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Per-(session, worker) ring buffer with replay on subscribe.

### AW-149 — Panel dot/badge vanish after refresh
**What happens.** The badge is a local 15s recency heuristic gated on `events.length > 0`, and `runtimeStatus` comes only from live events; the REST poll that does carry `runtime_status` is never passed to the panel, so a healthy worker looks idle after a refresh.
**Evidence.** `240636202e57`, badge_finding.
**Recurrence.** 1.
**Impact.** cosmetic.
**Suggested fix.** Green dot = session `isRunning`.

### AW-150 — Dead / stale telemetry paths
**What happens.** `agent/presenter/event_processor.py:78`'s `paused` handler has no emitter since Wave 7; the pause/resume JSX inside `events.map` in `WorkerOutputPanel` (L786-831) never renders because the list is empty; `worker_event_schema.json` is stale; `workerInfo.started` is dead. Each is a plausible-looking path that silently does nothing.
**Evidence.** `cffb1671448a` §06.
**Recurrence.** 3.
**Impact.** cosmetic.
**Suggested fix.** Delete the dead paths.

### AW-151 — Host-execution gate fails OPEN
**What happens.** `_host_execution_denied_reason` returns `None` — meaning *allowed* — when no workspace id resolves, so "no resolvable workspace id → legacy host path stays allowed". The gate's default is permissive.
**Evidence.** "git_info_tool host-execution gate investigation + parking lot" (`22df570cac07`), part 1 (`git_info_tool.py` L115-117).
**Recurrence.** 1.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-152 — Deny message renders a literal `{ws_id}`
**What happens.** The denial string at L122-126 is a plain string, not an f-string, so the operator sees `{ws_id}` instead of the workspace that was actually denied.
**Evidence.** `22df570cac07`, part 1 (L122-126).
**Recurrence.** 1.
**Impact.** cosmetic.
**Suggested fix.** none recorded.

---


## Theme 6 — Verification & trust

### AW-153 — Full-suite pytest hangs at node #1382
**What happens.** The full suite hangs (killed by the harness, `EXIT=124`) at node #1382 after 90–150s+ because a bare `None` stop sentinel no longer breaks `run()`. Twelve bare `put(None)` producers across four test files depend on the old contract. Full-suite verification is therefore unavailable and only per-file runs work.
**Evidence.** "hang_investigation_report" (`f35ae2ccc855`), verdict (2026-08-23).
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** Restore `break` for a bare `None` sentinel.

### AW-154 — No global pytest timeout or faulthandler config
**What happens.** Nothing in the pytest configuration imposes a per-test timeout or dumps stacks on hang, so any future hang blocks a verification run for as long as the outer harness allows.
**Evidence.** `f35ae2ccc855`, clean_head section.
**Recurrence.** 1.
**Impact.** blocks work.
**Suggested fix.** none recorded.

### AW-155 — Order-dependent flakiness
**What happens.** `test_real_thread_pause_resume_and_query_envelope` fails in the combined run and passes in isolation; three task4 failures were traced to a fixture wiping `ProviderFactory._providers`. Green isolated runs therefore say little about the suite.
**Evidence.** "Worker Lifecycle Refactor Tracker" (`65b5b92028cd`), test_results.
**Recurrence.** 3.
**Impact.** costs significant time.
**Suggested fix.** none recorded.

### AW-156 — Backwards `skipif` probes
**What happens.** Tests 6 and 8 gate on `hasattr(...)` seams that do not exist yet, so they currently run against fallback paths; once the seam lands the probe skips, which is a coverage loss rather than a gain. The probe direction is inverted.
**Evidence.** `65b5b92028cd` W2.
**Recurrence.** 1.
**Impact.** causes wrong output.
**Suggested fix.** Negate the probes in W3.

### AW-157 — Worker wrote a file into the repo during a read-only audit
**What happens.** Despite an explicit read-only constraint, a worker created a report file in the repository working tree during an audit; it had to be found and deleted afterwards.
**Evidence.** `719e62e85d56`, chunk A, ACTION ITEM.
**Recurrence.** 1.
**Impact.** costs significant time.
**Suggested fix.** none recorded.

### AW-158 — Worker self-reported the wrong repo and branch
**What happens.** A worker reported itself as working on `feat/resource-permission-separation` at `cfde827` when the actual state was `dev` at `b27a360`. Provenance claims in worker reports are not trustworthy on their own.
**Evidence.** `719e62e85d56`, chunk A, DISCREPANCY.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-159 — Verbatim citations produced with zero tool calls
**What happens.** A worker audit reported "Phase 2a: worker ran 0 tool calls" yet presented verbatim excerpts; the excerpts needed independent confirmation. A citation is not evidence that the file was read.
**Evidence.** `97d74f03cceb`, status (Phase 2a caveat).
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-160 — `DockerCodeRunner` gives a fresh container per call
**What happens.** Each call gets a new container, so `/tmp` state does not persist between calls in a chain. A multi-step verification written as separate calls can pass vacuously because each step starts clean.
**Evidence.** "Overnight autonomous run — anti-forgetting system, V2 phantom notification, duration metrics" (`d32c89eb0023`), phase1_dashboard.
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-161 — Phases marked "DONE" from unverified second-hand recon
**What happens.** Phases were reported complete on the strength of recon that was never checked, while the document's own definition of done states that documents are not proof. The gap is recorded in the document's own open-risks section.
**Evidence.** `d32c89eb0023`, open_risks.
**Recurrence.** 2.
**Impact.** costs significant time.
**Suggested fix.** Verify each claim against source before declaring a phase done.

### AW-162 — Worker died mid-dispatch leaving half-finished edits
**What happens.** A worker hit the 300s query cap while at critical tokens and was reclaimed with its containers, leaving a partial set of backend edits in the tree. A second instance had to verify and complete them.
**Evidence.** `17a5e833f598`, status (2026-08-24, default worker #2).
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** Force-respawn recovers (the standing workaround).

---

## Theme 7 — Orchestrator tooling

### AW-163 — Orchestrator turn limit forces mid-unit handoff
**What happens.** The orchestrator hits its turn limit mid-unit, so work in flight is handed to a fresh session whose only continuity is a job id and a doc section. In one case the handoff was recorded at 2026-08-19 20:40.
**Evidence.** "worker-operation-semantics-design-map" (`8330a7e9d3ff`), state section.
**Recurrence.** 3.
**Impact.** blocks work.
**Suggested fix.** none recorded.

### AW-164 — A standing workaround list instead of fixes
**What happens.** The document records the operative technique set as: bounded per-file pytest with an outer shell `timeout` of 240–300s, a ~60s tool budget per turn, chained Docker commands instead of separate calls, `submit_query` + `job_status` for async work, force-respawn to recover frozen workers, and full briefs in `query`. Each line is a workaround for a defect above.
**Evidence.** `8330a7e9d3ff`, state section.
**Recurrence.** 3.
**Impact.** costs significant time.
**Suggested fix.** none recorded.

### AW-165 — `Worker` tool documentation contradicts behaviour
**What happens.** The action docstring omits `stop` entirely (it lists list/spawn/check/query), documents no `briefing` field, and the field docs claim other context keys "are passed as metadata" when they are in fact dropped (AW-101). `worker.py` L2764-2795 confirms the fields that exist.
**Evidence.** "worker_cap_reset_continue_impl" (`9d5b6d43326f`); "worker-op-semantics-pass-b2-collected" (`1f57b70cb21e`), pydantic_fields (L2764-2795).
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** none recorded.

### AW-166 — Dead control-surface API and constants
**What happens.** `reset_worker` raises `WorkerControlError` until `worker.py` gains a `reset()` method, and `SPAWN_QUEUE_TIMEOUT` is defined for a "future queued spawn variant" that does not exist. Both look usable and are not.
**Evidence.** `65b5b92028cd` W2.
**Recurrence.** 1.
**Impact.** cosmetic.
**Suggested fix.** none recorded.

### AW-167 — The only real kill path ships disabled
**What happens.** WLM is the sole mechanism that can actually stop a running worker, and it is off by default; `ExecutionTracker` registration was inert until W3b1, so execution tracking protected nothing in the meantime.
**Evidence.** `719e62e85d56`; `65b5b92028cd`.
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** Register unconditionally (W3b1).

### AW-168 — S7c "announce-only" watchdog was a false safety invariant
**What happens.** The watchdog was documented as announce-only — it would not terminate anything. Because `global_event_bus` publishes synchronously, it would in fact have terminated the in-flight executions of any worker silent for more than 600s. A stated safety property that does not hold is worse than none.
**Evidence.** `cffb1671448a` §5.2 (commit `01ff586e`).
**Recurrence.** 1.
**Impact.** causes wrong output.
**Suggested fix.** Private `EventBus` plus `stale_callback=None`.

### AW-169 — `WorkerRuntimeHandle` never constructed in production
**What happens.** `workspace_routes.py:410` always misses, so the runtime handle is never built and the S5 path is dead in production while appearing wired.
**Evidence.** `cffb1671448a` §5.2.
**Recurrence.** 1.
**Impact.** blocks work.
**Suggested fix.** none recorded.

### AW-170 — C13: container-exec timeout kills the container
**What happens.** `ResourceContainerManager.exec` deliberately kills the container on timeout. Every subsequent command then resolves a dead container by label and fails, until a human intervenes — one slow command poisons an entire worker session.
**Evidence.** `cffb1671448a` §1.4 (2026-11-09).
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** Filed open (C13).

### AW-171 — `/tmp` is a 64M tmpfs
**What happens.** The container's `/tmp` is a 64M tmpfs while the workspace offers 368G, so full-tree copies into `/tmp` fail with `ENOSPC`. Verification steps that stage a tree there silently cannot run.
**Evidence.** `f35ae2ccc855`, repro note.
**Recurrence.** 1.
**Impact.** costs significant time.
**Suggested fix.** `tar` copy excluding `.venv`, `.git`, `logs`.

### AW-172 — Sandbox lacks node/npm/network/root
**What happens.** Frontend tests and builds cannot run in the execution sandbox at all and must be deferred to host or CI, which turns frontend work into an unverifiable claim inside an otherwise verified pipeline.
**Evidence.** `17a5e833f598`, status.
**Recurrence.** 2.
**Impact.** blocks work.
**Suggested fix.** none recorded.

### AW-173 — Frontend vitest fails spuriously without `TMPDIR=/tmp`
**What happens.** jsdom/threads in the frontend test setup fail unless `TMPDIR=/tmp` is exported; the failures look like real test defects and have to be remembered as environment noise.
**Evidence.** "W6 implementation brief — detached workers + visual grain" (`870b1c658b7e`), §2.2.
**Recurrence.** 1.
**Impact.** costs significant time.
**Suggested fix.** Run with `TMPDIR=/tmp`.

### AW-174 — Docs cite `path:LINE` premises that need scripted verification
**What happens.** Implementation briefs cite code premises as `path:LINE`, and the brief requires a Phase 0 to verify them, treating a false premise as a STOP condition. The very existence of that phase implies prior dispatches implemented against unverified premises.
**Evidence.** `870b1c658b7e`, Phase 0.
**Recurrence.** 2.
**Impact.** costs significant time.
**Suggested fix.** `scripts/verify_doc_citations.py`.

### AW-175 — Phantom config-change notification
**What happens.** `base_url` is compared with a raw `!=` in three places (`agent.py:523/591/552`), so a one-character difference forces a full agent restart plus a `[SYSTEM NOTIFICATION] Configuration updated` user-role message that then lives in the LLM context forever. The two defaults genuinely differ by one character: `config_manager.py:61` ends `".../v1/"` and `models.py:92` ends `".../v1"`.
**Evidence.** `d32c89eb0023`, recon section (2026-10-30).
**Recurrence.** 2.
**Impact.** causes wrong output.
**Suggested fix.** Normalise before comparing (strip, lower, rstrip `'/'`).

### AW-176 — Committed merge-conflict block in `.gitignore`
**What happens.** An unresolved merge-conflict block sits committed in `.gitignore` around L301-304, so ignore rules after it are ambiguous and every future edit to the file inherits the conflict.
**Evidence.** `d32c89eb0023`, phase1_dashboard.
**Recurrence.** 1.
**Impact.** cosmetic.
**Suggested fix.** none recorded.

---

## Summary table

Severity mapping: **High** = blocks work *or* causes wrong output; **Medium** = costs significant time; **Low** = cosmetic.
Theme keys: D&B = Dispatch & briefings, T&L = Timeout & turn lifecycle, PSD = Pause/stop/detach/revive, C&T = Context & token ceilings, E&T = Envelope & telemetry truthfulness, V&T = Verification & trust, OT = Orchestrator tooling.

| ID | Theme | Title | Rec. | Sev. |
|---|---|---|---|---|
| AW-101 | D&B | Non-`query` context keys silently dropped | 6 | High |
| AW-102 | D&B | Real task in `briefing`, stub in `query` → wrong file edited | 2 | High |
| AW-103 | D&B | Brief eaten by worker summarisation mid-task | 2 | High |
| AW-104 | D&B | Brief has no delivery receipt or ack | 3 | High |
| AW-105 | D&B | `reset()` destroys brief; re-context duplicates; nested `meta` dropped | 3 | High |
| AW-106 | D&B | `spawn(force=True)` returns `spawned:false` with no error | 2 | High |
| AW-107 | D&B | Workers can never ask (`NullEventBus.ask()` denies) | 3 | High |
| AW-108 | D&B | Worker `host_bash` ask prompts the operator (120s) | 2 | High |
| AW-109 | D&B | CheckSystem/Worker/KB tools unavailable to workers | 3 | Medium |
| AW-110 | D&B | Orchestrator sends workers to nonexistent paths | 2 | Medium |
| AW-111 | T&L | Four timeout roles conflated, never unified | 4 | High |
| AW-112 | T&L | Soft timeout never detected: `'critical'` vs `'CRITICAL'` | 3 | High |
| AW-113 | T&L | `timeout_seconds` ignored; spawn onto live instance dropped query | 3 | High |
| AW-114 | T&L | Harness caps response window ~150/409/540s + ~480s hard kill | 3 | High |
| AW-115 | T&L | No thread kill exists; 600s/300s are queue and cutoff only | 5 | High |
| AW-116 | T&L | Pause/stop never fire during a tool call | 3 | High |
| AW-117 | T&L | Main agent cannot stop a blocked worker | 3 | High |
| AW-118 | T&L | Soft-timeout at 333.9s vs 300s budget, answer still complete | 1 | Medium |
| AW-119 | T&L | No dedicated stop-propagation test | 1 | High |
| AW-120 | PSD | Detach/stop mid-flight loses the in-flight tool result | 3 | High |
| AW-121 | PSD | `pause()` discarded mid-turn work (2/2 failures) | 2 | High |
| AW-122 | PSD | Mid-turn pause emits and writes status twice | 2 | Low |
| AW-123 | PSD | Global pause pauses ASYNC workers too | 2 | High |
| AW-124 | PSD | Any follow-up query auto-resumes a paused worker | 2 | High |
| AW-125 | PSD | Detached instance keeps its `instance_id` slot | 3 | High |
| AW-126 | PSD | `_WorkerAmbiguityError` on name collisions | 1 | Medium |
| AW-127 | C&T | Context doubling fixed then disproven (+1,987/+2,752/+454) | 4 | High |
| AW-128 | C&T | Reported tokens are TOTAL prompt, not conversation | 2 | High |
| AW-129 | C&T | Warning gate exact, message floored: fires at "67k" vs 65,000 | 2 | Low |
| AW-130 | C&T | WARNING tier does not restrict tools | 2 | High |
| AW-131 | C&T | Worker blocked from writing its own working doc | 2 | Medium |
| AW-132 | C&T | Stop paths persisted uncompacted pre-summary history | 2 | High |
| AW-133 | C&T | No mid-query checkpointing | 2 | High |
| AW-134 | C&T | Tool output capped at 10,000 tokens; unreadable 10.4MB/9.1MB files | 2 | Medium |
| AW-135 | E&T | `query` returns the STALE previous answer instantly | 3 | High |
| AW-136 | E&T | "One query behind" desync family | 4 | High |
| AW-137 | E&T | Drained query returned under new id with null metadata | 2 | High |
| AW-138 | E&T | stop-mid-query envelope has `status: null` | 2 | High |
| AW-139 | E&T | Worker receives `None` for status/confidence/meta | 2 | High |
| AW-140 | E&T | `status` has ~17 writers, no owner, no CAS | 3 | High |
| AW-141 | E&T | `paused` status can survive `kill()` | 2 | High |
| AW-142 | E&T | Restriction written outside the state API (:3754-3756) | 2 | Medium |
| AW-143 | E&T | Two `command.json` readers race | 2 | High |
| AW-144 | E&T | Six event-egress families; hooks cover 2 | 2 | High |
| AW-145 | E&T | Per-worker bus lazily created; silent drop while adapter `None` | 2 | Low |
| AW-146 | E&T | Work events carry no `instance_id` → dropped by instance filter | 2 | High |
| AW-147 | E&T | `worker_partial_result` unconditionally dropped | 2 | High |
| AW-148 | E&T | Panel empty after F5 until next `context_summarized` | 2 | High |
| AW-149 | E&T | Panel dot/badge vanish after refresh | 1 | Low |
| AW-150 | E&T | Dead/stale telemetry paths | 3 | Low |
| AW-151 | E&T | Host-execution gate fails OPEN | 1 | High |
| AW-152 | E&T | Deny message renders literal `{ws_id}` | 1 | Low |
| AW-153 | V&T | Full-suite pytest hangs at node #1382 (EXIT=124) | 2 | High |
| AW-154 | V&T | No global pytest timeout/faulthandler | 1 | High |
| AW-155 | V&T | Order-dependent flakiness | 3 | Medium |
| AW-156 | V&T | Backwards `skipif` probes | 1 | High |
| AW-157 | V&T | Worker wrote a file during a read-only audit | 1 | Medium |
| AW-158 | V&T | Worker self-reported wrong repo/branch | 2 | High |
| AW-159 | V&T | Verbatim citations with zero tool calls | 2 | High |
| AW-160 | V&T | Fresh container per call; `/tmp` does not persist | 2 | High |
| AW-161 | V&T | "DONE" from unverified second-hand recon | 2 | Medium |
| AW-162 | V&T | Worker died mid-dispatch leaving half-finished edits | 2 | High |
| AW-163 | OT | Orchestrator turn limit forces mid-unit handoff | 3 | High |
| AW-164 | OT | Standing workaround list instead of fixes | 3 | Medium |
| AW-165 | OT | `Worker` docstring omits `stop`; misstates context handling | 2 | High |
| AW-166 | OT | `reset_worker` unusable; `SPAWN_QUEUE_TIMEOUT` reserved | 1 | Low |
| AW-167 | OT | WLM off by default; ExecutionTracker inert | 2 | High |
| AW-168 | OT | S7c "announce-only" watchdog was a false invariant | 1 | High |
| AW-169 | OT | `WorkerRuntimeHandle` never constructed in production | 1 | High |
| AW-170 | OT | C13: exec timeout kills the container | 2 | High |
| AW-171 | OT | `/tmp` is a 64M tmpfs | 1 | Medium |
| AW-172 | OT | Sandbox lacks node/npm/network/root | 2 | High |
| AW-173 | OT | Frontend vitest needs `TMPDIR=/tmp` | 1 | Medium |
| AW-174 | OT | Docs cite `path:LINE` premises needing verification | 2 | Medium |
| AW-175 | OT | Phantom config notification; defaults differ by one char | 2 | High |
| AW-176 | OT | Committed merge-conflict block in `.gitignore` | 1 | Low |

---

## Limitations

- **Coverage.** `.thoughtmachine/working_docs/` holds roughly 340 `*.json` working documents; 24 were read in full. Everything below comes from that 24-document sample, so per-theme counts and the total are a floor, not a census. Documents not read (highest-value candidates first): `777ed7084045`, `f46caa2d8e89`, `b7dad979aa17`, `9ebfe2e0e970`, `e5a7c4f4c854`, `6e6f1071cebd`, `6a312732b66c`, `2a6ea19910c6`, `e831b50c3d6b`, `3fdc31553929`, `978af0363a5b`, `c2b77c25a1e2`, `85b1136e3265`, `038dba57ce20`, `40fb6b44ad27`, `30c279796f0b`, `cd553438ee14`, `cd3bece07fe6`, `b3e2b089f492`, `f2996b35a539`, `619ae0ec5455`, `75cf90d03484`, `65edd7db427a`, `97250abe8d76`, `dc6bf8cba328`, `47ba8dacf7cb`, `c5074b29c6d2`, `14a24dd0034d`, `6e0913aecb8b`, `58d044093952`.
- **No new verification.** This is a harvest, not an audit. No claim here was re-checked against source code; each is attributed to the working document that recorded it, and several are second-hand accounts by workers that were themselves unreliable (see AW-158, AW-159, AW-161).
- **Some items are self-disproving.** AW-127 records a fix that a later experiment disproved; AW-168 records a safety invariant that was false. They are listed because the *process* failure is the defect, not because their conclusions hold.
- **Recurrence counts are as written in the source documents**, not recomputed from logs. Where a document gives a single sighting, recurrence is 1 and the item should be read as anecdotal.
- **Overlap with Part 1.** Part 1 (`AW-001`–`AW-081`) harvested other sources; some themes recur there. No attempt was made to de-duplicate across parts, since cross-part repetition is itself evidence of recurrence.
- **Dates** are the dates recorded in the documents, not dates on which this report was written. The corpus spans 2026-07-27 to 2026-11-09.
- **Severity is a mapping, not a judgement call per item**: blocks work and causes wrong output were both flattened to High, which makes the table coarser than the Impact field inside each item.

