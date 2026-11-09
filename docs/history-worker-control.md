# Worker Control: How We Got Here

*Timeouts · pause/stop/kill · the briefing channel · and the event-driven rewrite*

**Purpose.** Background document for the event-driven worker-control redesign. It is *archaeology*, not a spec: what each mechanism was originally for, when it changed shape, and which defects survived into today's code.

**How to read the citations.** Dates are session dates from the project knowledge base (`project/system_architecture.md`, `personal/bugs_and_fixes.md`, `personal/task_tracker.md`, `project/status.md`). `file:line` references are correct **as of the date cited**, not at HEAD — this codebase moves daily and line numbers rot.

---

## 00 — Summary (the whole story in one page)

The worker is a **cooperative agent thread with no kill switch**. Everything the product calls "timeout", "pause", "stop" and "kill" is a naming layer over exactly two primitives:

1. **Restriction** — `AgentState.restrict(reason)` makes `get_allowed_tools()` return `['Respond']`. The agent is still running; it simply gets refused at the tool-executor gate (`tool_executor.py:272`). Nothing is interrupted.
2. **Parking** — an `threading.Event` (`_park_event`) gates new queries. The thread sleeps between turns and is woken by an input queue / command file.

Every difficult invariant in this area follows from that: a "paused" worker is *restricted and parked*, not suspended; a "timed-out" worker keeps running and keeps writing `context.json`; a "killed" worker is a latch (`_stop_event`) that is never cleared plus a status field that ~17 separate sites write. The mechanism family has been rebuilt three times (halt-style pause → checkpoint-style pause → reading-mode pause, with stop going from session-scoped to turn-scoped) and each rebuild fixed the layer above while leaving the primitive underneath.

**Three clocks were conflated early and never unified.** *(a)* the worker's own soft budget (cooperative, default `WORKER_TIMEOUT_SECONDS=600`), *(b)* the caller's blocking wait (`SPAWN_QUEUE_TIMEOUT=600`, `300s` for query, later `+60s` grace), *(c)* the platform/harness cap on the blocking response window (~150s / ~409s / ~540s observed in different environments, plus a ~480s hard kill). The worker never learns that (b) or (c) expired, which is the origin of the "one query behind" stale-answer bug and of the context-doubling bug (68k → 136k) when a timed-out worker is force-respawned.

**The briefing channel was never a delivery feature.** `Worker(context={...})` sends a JSON blob whose only load-bearing key is `query`; the rest arrives — if it arrives — as a second `role:"system"` message `"Initial context: {json}"`. The harness drops non-`query` keys in transit (observed repeatedly, 2026-08-02 → 2026-11-03). The halting rule that followed — **briefs must be persisted *and* inlined** (working agreement, 2026-09-19) — is a workaround for a missing acknowledgement, not a fix. Compounding it: a brief is a *message*, so worker-side summarisation can eat it mid-task, and nothing re-asserts it. The channel is neither guaranteed-delivered nor guaranteed-retained, and **nothing reports either failure**.

**The rewrite in flight** replaces both primitives with a framed wire: `worker_runtime_protocol.py` (S0, `v:1` envelope, per-direction `seq`, worker/instance/epoch attribution in the envelope), process transport over a `socketpair` (S2), the event seam (S4a/S4b), config/permission frames, and an announce-only stale watchdog (S7c). The historical requirement it must satisfy is simple to state and hard to keep: **one clock, one writer per state field, and every control action a frame with an ack** — because everything that has gone wrong here went wrong because a second party could not tell what the first party had already done.

## 01 — Worker timeout logic

### 1.1 There are four timeouts, and only one of them is "the timeout"

| # | Role | What it actually is | Default / value | Where |
|---|---|---|---|---|
| 1 | **Worker soft budget** | Cooperative: `TimeState` becomes `CRITICAL` when `elapsed >= timeout_seconds`; `get_allowed_tools()` then returns `['Respond']`. The worker is *told* to wrap up. | `WORKER_TIMEOUT_SECONDS = 600` in `agent/config/defaults.py:59`; resolution chain ctor arg > worker definition > `agent_config.worker_timeout_seconds` > `session_config` > default (`worker_thread.py:886-904`, 2026-09-08) | `agent/core/state.py` (`update_time_state`, restriction at ~228-231 / 400-402) |
| 2 | **Caller blocking wait** | How long the *tool call* waits for a result before returning. Distinct code path, distinct constant. | `SPAWN_QUEUE_TIMEOUT = 600` on spawn auto-query (`tools/workspace/worker.py:~2237`); `300s` cutoff on the `query` action (~2366); later `QUERY_WAIT_GRACE_SECONDS = 60` added (2026-08-29) | `tools/workspace/worker.py` |
| 3 | **Main-agent soft budget** | Same mechanism as #1, for the orchestrator. | `agent_soft_budget_seconds = 300` (`resources/default_config.json:16`), `SOFT_BUDGET_FALLBACK_SECONDS = 300` (`timeout_constants.py:28`) | agent config |
| 4 | **Hard backstop (WLM)** | The only thing that can actually terminate: the Workspace Lifecycle Manager. **Off by default.** | `SOFT/HARD/EXEC_KILL_GRACE` at `infra/workspace_lifecycle_manager.py:84`, enforced `:429-437` | infra |

Plus a fifth, outside the repo: the **harness cap on the blocking response window** — observed at ~150s, ~409s and ~540s in different environments, with a ~480s hard kill that returns nothing. It never kills the worker; it only stops *us* from hearing from it (2026-10-09, 2026-11-03).

Warning threshold: `time_warning_threshold` default **240s** (injects a warning the LLM actually sees, immediately, not buffered).

### 1.2 Chronology

- **2026-08-05 — first forced-finish observations.** Workers resumed after token pruning "flake"; envelopes come back at ~300–325s with content that is clearly a wrap-up, not an answer.
- **2026-08-06 — the cap and the stale answer.** Hard platform cap ≈600s, worker keeps running → desync: *a query sent while the worker is busy is answered with the worker's OLD-task output* ("one query behind"). This is the first sighting of the family that still bites today.
- **2026-08-07 — mechanics confirmed.** Practical rule adopted: set soft `timeout_seconds ≤ 300` so the worker wraps up *before* the caller's cap, whatever the cap is.
- **2026-08-10 — audit + 4 fixes (the turning point).** Discoveries, all still true: **no worker thread kill exists**; "hard timeout" is caller-side only; the daemon thread keeps running *and keeps saving `context.json`*. Root cause of **context doubling 68k → 136k**: a force-respawn reloads the *entire* timed-out attempt, then runs the new query on top (~2×). Secondary causes: stale re-put at queue tail (paused-resume drained FIFO), stale-thread overwrite race. Fixes **F1–F4**: prune-on-timeout, generation counter, `compact_after_summary`, resume drains new-query-first. Regression suite `tests/test_worker_timeout_audit.py` (14 passed).
- **2026-08-25/26 — claim audit.** Re-verified against code: no `kill()` on `WorkerThread`; `stop()` = status `'stopping'` + `_stop_event` + `command.json`; WLM off by default so nothing hard-kills; *the worker's own soft timeout is what ends an attempt.* Roadmap item logged: **"timeout unification (4 roles)"** — still open.
- **2026-08-29 — dispatch 2** (`fix/worker-timeout-and-stale-instance`), three defects in `tools/workspace/worker.py`: **D1** requested `timeout_seconds` was not respected (fixed; +`QUERY_WAIT_GRACE_SECONDS=60`); **D2** spawning onto a live, non-paused instance silently dropped the query (fixed: reuse → deliver-and-block); **D3** soft-timeout detection rebuilt as `_worker_timeout_detected()`. D3's root cause is the single most instructive bug in this whole area: **production `TimeState` values are lowercase (`'critical'`), while the legacy comparison checked for uppercase `'CRITICAL'` — so soft timeouts were silently never detected.** Fixed by also checking `restriction_reason == 'timeout'` and `elapsed >= budget`. 52 passed / 2 skipped; full suite 2150 passed.
- **2026-09-08 — "no 60-second default exists" investigation.** The chain in §1.1 row 1 was written down properly for the first time; confirmed the soft restriction is `TimeState.CRITICAL` → `allowed=['Respond']`.
- **2026-09-16 — by-design ruling.** "Soft timeout invisible → **by design**: soft timeout is a cooperative tool restriction (`state.py:392-407`, enforced `tool_executor.py:208`), not a thread kill; the hard backstop is WLM." Useful, and also the moment the product's expectation and the implementation's truth were officially allowed to differ.
- **2026-10-09 / 2026-11-03 — transport caps.** The blocking response window is capped regardless of `timeout_seconds` (~540s one place, ~150s another). Both entries carry the same lesson: *the number you configured is not the number you observed.*

### 1.3 The invariant that survived all of it

**Cooperative restriction is the only in-process timeout mechanism.** Nothing kills the thread. Therefore:
- A timed-out worker is still running, still holding its `context.json`, still able to answer a *later* query with an *earlier* task's result.
- The caller's wait and the worker's budget are independent clocks with no shared deadline and no handshake.
- Any "timeout" observed from outside may be the transport (harness), not the worker — and the two are indistinguishable from the envelope.

### 1.4 Still open

- **Timeout unification across the 4 roles** — roadmap item, untouched since 2026-08-25.
- **C13 (2026-11-09)** — `ResourceContainerManager.exec` timeout path *kills* the container deliberately (not removes), so every later command on that workspace resolves a dead container by label and fails until a human intervenes. Filed open.

## 02 — Pause / Stop / Kill: four rebuilds

### 2.1 Era 1 (Mar–Apr 2026) — pause as a halt

The original design: `request_pause()` sets `_pause_requested`; the turn loop checks it at **three checkpoints** — *(1)* `turn_start`, *(2)* `after_llm`, *(3)* `after_turn` → `PAUSING` + a `paused` event to the GUI. Architecture was 3-layer (Agent → Controller → GUI), and the era's stated achievement was "thread-safe control with synchronization primitives" (`project/system_architecture.md`, *Project Historical Trajectory*).

Checkpoint 2 is where the design got its permanent complication: if the LLM has returned `tool_calls`, honouring the pause immediately would orphan them, so the pause is **DEFERRED** and one **grace turn** is allowed — the assistant message is committed *without* `tool_calls`. Pause therefore never means "stop now"; it means "stop at the next consistent boundary, plus maybe one more turn".

Roadmap Phase 3 (still visible in `roadmap`): **3.1** preserve grace turn on pause (commit content *before* yielding `paused`), **3.2** "Pausing…" display timing, **3.3** verify every `stop_reason` banner, **3.4** delete dead `ExecutionState` branches. Items 3.1/3.2 are the ancestors of today's ordering bugs.

### 2.2 Era 2 (Aug 2026) — the checkpoints start leaking

Verified deep-dive 2026-08-01 (`agent.py:823-1384`, `state.py:406`, `tool_executor.py:404`): the pause path is as above, and the pause *event* is emitted by the loop, not by the request. So the requester and the observer are already out of sync by one turn. Everything later called "pause is a lie" starts here.

### 2.3 Era 3 (Wave 7, 2026-11-02) — "Pause = reading mode, not a halt"

Commits `b9c550ce`, `8ca03339`, `04a4f41d`, `0565aec1`. The mechanism was rewired around state, not around checkpoints:

- `AgentState.restrict(reason)` / `clear_restriction(reason=None)` (`state.py:~392 / :421`). **Ownership-on-clear**: `clear_restriction` is a **no-op unless `restriction_reason == reason`**. That single line makes cross-thread pause/resume safe without a lock, and lets a `'token'` restriction survive a `'pause'` resume.
- `get_allowed_tools()` is a **fail-safe map**: only `'token'` → `['Respond','SummarizeTool']`; *every other reason* — `pause`, `turn`, `timeout`, unknown, `None` — → `['Respond']`.
- Stopping tools takes **two ingredients**: (1) the executor refuses with prose at `tool_executor.py:272`; (2) `enter_reading_mode('pause')` buffers exactly ONE `[SYSTEM NOTIFICATION]` nudge on `_pending_warnings`, flushed after `turn_transaction.commit()` (`agent.py:1894`), so it lands *after* tool results. There is **no inject / steer / queue API** — the nudge is the whole notification surface.
- **Cross-thread invariant (load-bearing):** `enter_reading_mode` / `leave_reading_mode` may be called from *any* thread, but they only set flags; every `Message` object is constructed on the agent's turn thread.
- **Park is separate from restriction.** `request_pause()` still sets `_park_event`; `_park_event` is the idle gate for new queries (~15 sites). `status='paused'` is written by the **run loop after it parks**, not by `pause()` — hence a live window where the agent is *restricted but reads busy/ready*.
- **`paused` event emitter deleted.** The after-turn checkpoint no longer halts (it was deleted), so the last emitter of the `paused` event went with it; `agent/presenter/event_processor.py:78` (`elif event_type == 'paused'`) is now **dead code**.

**Stop was re-scoped in the same wave (`b9c550ce`): "Stop is turn-scoped."** `bridge._stop_session_workers` deleted. `request_stop()` now = set a turn token → `_count_session_workers()` → controller `request_halt()` → legacy `_stop_event`. **Session workers keep running.** `POST /workers/stop_all` was redefined as **DETACH** (park, keep thread, keep registration, keep queued work). The per-worker kill route is the **only** terminating path in the product.

**Wave-7 cancellation isolation (`9c77577c`).** A third signal was added: `_turn_cancel`, per-worker per-turn, bound thread-locally via `tools.sleep_tool.bind_turn_cancel`. `_stop_event` remains a **kill latch that is never cleared**. The original cut used a process-global `Event`, so one worker's kill aborted another worker's `sleep` — fixed by making it thread-scoped. `sleep` tool capped at 300s.

**P2 fenced command channel** (2026-11-02): every worker-control command file is fenced on 4 axes and consumed by exactly one incarnation — `tools/workspace/command_channel.py`, plus `_write_worker_command(...)` / `_read_worker_generation` in `workspace_routes.py`. Type strictness is deliberate: producers must write `session_id=""`, not `None` (`""` is obeyed, `null` is rejected). **This fence was a hard prerequisite for the process-transport work**, because a second reader of `command.json` is only safe once the fence exists.

### 2.4 The vocabulary is the problem

| Word (UI) | What the code does | Terminates? |
|---|---|---|
| **Pause** | `restrict('pause')` + park; reading mode; no tools except `Respond` | No |
| **Detach** (`stop_all`) | Park + keep thread/registration/queue (`detach()` reuses `_pause_event`) | No |
| **Stop** | Turn token → `request_halt()` → `_stop_event` for *this turn*; session workers unaffected | No |
| **Kill** | Per-worker route; `_stop_event` is a latch never cleared | Intent: yes. Reality: last writer of `status` wins (see §3) |
| **WLM kill** | `infra/workspace_lifecycle_manager.py` | Yes — **off by default** |

"Pause removal" is a recorded open item: pause as a *user verb* should go (340 non-test + 118 frontend + 320 test hits), while the *primitive* must stay, because `detach()` is built on it.

### 2.5 What the pause gate actually does — measured, not inferred

Verified live in this session rather than from notes: the gate is real (`tool_executor.py:272` → `state.is_tool_allowed` → `get_allowed_tools()` returns `['Respond']` for `'pause'`, `state.py:469-485`). Commands that *appeared* to run after pause were a **TOCTOU race**, not a missing gate. Conversely, a **queued worker query** that drained after the pause executed later and its result was returned **under the new query id** with `status/confidence/meta: null` — i.e. a result-misattribution bug whenever pause defers a queue. Both facts matter for the redesign: the gate is sound; the *bookkeeping around the gate* is not.

## 03 — What is wrong with workers today

Five classes. They are listed by *mechanism*, because that is what the rewrite can fix.

### Class A — Control-plane incoherence (the state field is a rumour)

- **`status` has ~17 writers** in `worker_thread.py` (writes at 1784, 1810, 1843, 2027, 2523, 2582, 2978, 3373, 3542, 3597, 3711, 3784, 3871, 3927, 4060, 4100, 4307 as of 2026-11-09). No single owner, no CAS, no "who may write this".
- **`paused` can survive `kill()`** (unconfirmed P0, 2026-11-09). Two candidate writers after a kill: `worker_thread.py:2974-2978` (`if self._park_event.is_set(): … status="paused"`) has **no `_stop_event` guard**; and `_poll_command` runs on the run-loop thread *inside* the park wait (`:3576-3593`), so **two consumers race for `command.json`** — the loser sees `claim is None` (`:2480-2481`) and returns without writing any status, leaving whatever was there.
- **Restriction can be written outside the state API.** `worker_thread.py:3754-3756` assigns `restrictions_active` / `restriction_reason` **directly** on `self._agent.state`, bypassing `restrict()` / `clear_restriction()`. This is the only live write outside `state.py`, and by construction it is invisible to instrumentation placed in those methods. A whole generation of investigation (Gen 61 dispatch 13) failed because a test was written against the *direct* path and correctly proved the state object was identical — while the real occurrences were where a **second** state object exists (`agent.py:497/802` build `ToolExecutor(state=None)` and patch later; `agent.py:948 _restart_with_config`; `worker_thread.py:3704` lazy worker build; `agent/controller/__init__.py` pause/resume path).
- **Restricted ≠ parked ≠ reported.** Because park and restriction are set at different times by different code, the UI can show *ready* while the agent is restricted, and *paused* while `pause()` was never called (only `kill()` was).

### Class B — Time and queue semantics

- Two clocks (§1.3): caller wait vs worker budget; plus the transport cap. No shared deadline, no handshake.
- **"One query behind"**: a query issued while the worker is busy is answered with the previous task's output (2026-08-06, still reproducibly observed).
- **Force-respawn doubles context** (68k → 136k) because the timed-out attempt is reloaded and the new query stacked on top (2026-08-10).
- **Queued-query misattribution** when pause/detach defers a queue (live, this session).
- **`spawn(force=True)` can return `spawned: false, status: busy`** with no error, so a dispatch silently never lands, and a follow-up `query` answers the *previous* task coherently and confidently (2026-11-04 ticket).

### Class C — There is no event egress choke point

Six distinct egress families in `WorkerThread` (2026-11-03): `_publish_event`, `_publish_global_event`, **nine** inline `global_event_bus.publish` sites, **three** direct `_publish_global_worker_event` calls, `WorkerBusAdapter._publish` (`:624`), and the `WorkerBusAdapter.state` setter (`:403`, publishes `WORKER_STATUS` at `:420`). Hooking the two helpers covers 2 of 6. The only genuine choke point is **`EventBus.publish`** (`agent/events.py:486`) — which has **no publish hook**; the workable seam is `subscribe(None, cb)` (wildcard).

Two consequences worth writing down: the per-worker bus is created lazily *inside the thread body* (`worker_thread.py:2763`), while `__init__` sets `None` (`:1176`), so the child attaches with a **10 ms poll for up to 5 s and then gives up silently** (accepted gap, 2026-11-03, to be fixed when the old thread loop is removed); and while `_worker_bus_adapter` is `None`, agent-stream events are **dropped silently** — no queue, no retry, no log (`:2442`, `:2565`).

### Class D — The briefing channel (§4 in full)

Briefs are not reliably delivered *and* not reliably retained, and no layer reports either failure.

### Class E — Runtime residue

C13 killed-but-retained containers (§1.4); `_worker_registry` survives a poll-kill; the ungated visual artifact write; `ci` missing from `_WORKSPACE_RESOURCE_MAP` (fail-open class). Each is a "we killed something and forgot the other half of it" bug — the same shape as Class A, one layer down.

## 04 — The briefing channel: the operator's suspicion, examined

### 4.1 What the channel actually is

`Worker(action="spawn", worker_name=…, context={"query": …, "briefing": …})`.

- The **only** load-bearing key is `query`. Everything else is best-effort.
- What arrives, when it arrives, is a **second `role:"system"` message** with the literal prefix `"Initial context: {json}"`.
- There is **no length cap**; truncation **omits but never invents**; `reset()` **destroys** the brief; re-context **stacks duplicate blocks** (preserved facts across several generations).
- A `meta` dict nested inside `context` is **silently dropped** (Gen 60 lesson).

So the brief has never been a delivery feature with a receipt. It is a payload that happens to be travelling in a wrapper the transport does not promise to preserve.

### 4.2 The governing workaround

**Working agreement, 2026-09-19: "BRIEFS MUST BE PERSISTED *AND* INLINED."** The reasoning is exactly the two failure modes:

> The harness drops `context["briefing"]` (and other non-`query` keys) in transit — so a persisted-but-not-inlined brief never reaches the worker; and an inlined-but-not-persisted brief vanishes with the orchestrator's context window. **Both halves are required.**

That is a correct rule, and it is also an admission: the channel cannot be trusted to carry a document.

### 4.3 Sighting log (independent, repeated, same shape)

| Date | What was seen |
|---|---|
| 2026-08-02 | Worker went looking for a "briefing document" on the filesystem because it never received one. |
| 2026-08-05 | Same class, recorded in passing. |
| 2026-08-29 | Worker rebuilt the design from repo state instead of the brief → off-spec test names. |
| 2026-11-03 | "Every spawn this session delivered only the `query` key." A 5-path brief with the 5th path only in `briefing` arrived as a 4-item task. |
| run_case **A6** | "LOST briefing PAYLOAD (silent compaction drop) … the worker never receives it while the operator and the spawning agent still believe it was briefed." |
| 2026-11-05 | **Pendulum Lab ruling**: the invented numbers were confabulation, not a delivery bug — "the briefing channel is exonerated" *for that case*. |
| 2026-11-09 | A dispatch put the real task in `briefing` and a stub in `query` → the worker received the stub, concluded "query truncated", reconstructed from commit messages, and edited the **wrong test file**. |

### 4.4 Two different problems wearing one name

Separating them is the whole value of this section:

**(i) Transport — keys other than `query` are not reliably delivered.** Structural, outside the repo's control, therefore not fixable by us. This is what the persisted-and-inlined rule routes around. Every sighting in the table above is consistent with this.

**(ii) Retention — the brief is a *message*, so it ages.** Once delivered, it is one system message in a growing history. Worker-side summarisation can **eat it mid-task** (a 15-item recon brief was summarised away; items 1–8 of the output were lost). Nothing re-asserts it. Nothing counts it. Truncation is silent. There is no delivery receipt, no ack, and no way for the brief *author* to learn that the brief no longer exists.

**Verdict.** The operator's instinct is right, but the defect is deeper than the field name. `briefing` is not "a broken variable"; it is **an unacknowledged, non-idempotent, non-versioned message** being asked to do the job of *state*. While it stays a message: it can be dropped on the way in (i), eaten on the way through (ii), and neither failure is observable at either end. The fix is not a better `context` dict — it is briefs with a **version, an ack, and a restatement point**, i.e. the event-driven treatment: state that is pushed on every wake-up and reconciled, not a payload that is hoped-for once.

**Adjacent member of the same family, filed OPEN (2026-11-05):** permission changes do not reach a worker that is not currently running a query. Suspected cause: permission state is pushed via the `EventBus` and a spawned-but-sleeping worker does not drain it until its next wake. The recorded question is exactly the briefing question: **lazy (read effective permissions at query start; the push is an optimisation) or eager (the push must wake/inject into a parked worker)**. Recorded note stands: this is the same family as the additive-injection questions already filed against worker briefing / context delivery. It needs a ruling *before* the event-driven design is frozen, because it decides whether "push" is load-bearing.

### 4.5 Confounds to keep in mind, so the history is not misread

- **Confabulation is not a delivery bug** (2026-11-05). A worker that invents plausible content may have received a perfect brief.
- **An empty-feeling answer may be the transport, not the brief** — see §1.2/1.3: the caller's cap can return the worker's *previous* output or a forced wrap-up.
- **A brief can be lost to summarisation after a successful delivery.** Symptom-identical, cause completely different, different fix.

## 05 — The event-driven rewrite, and the stages behind it

### 5.1 What is being replaced (the substrate)

| Piece | Today | Why it hurts |
|---|---|---|
| Transport | Worker runs as a **thread in the API server process** | A cancellation token cannot cross a real boundary; a hostile/buggy worker can take the server with it (hence `pids_limit`, W7) |
| Control | `command.json` on disk, **polled** | Two consumers race for it (`:3576` run-loop park wait vs `:3586` poll); loser returns silently. Fenced since P2, but still a file, not a message |
| Status | 17 assignments to `self.status` | Last writer wins; `paused` can outlive `kill()` |
| Events | Six egress families; no bus publish hook | Nothing can observe everything; child-side bus attach is a 5 s bounded poll |
| Security prompts | `NullEventBus.ask()` → **"deny"** instantly for workers | Workers can never ask; `ask` degrades to deny by design |
| UI control | HTTP only, no WebSocket control commands | Poll-based panels; push-first work unblocked by container-lifecycle events |

### 5.2 The staged plan (W7 → P3 S0…S7c), and what each stage settled

- **W7 — Process isolation & security hardening** (in progress 2026-10-28; control plane merged `#278`). Three items: move workers out of the API server's threads (so cancellation crosses a real boundary); `pids_limit` on the container so a fork bomb dies inside it; route the Visual tool's host write through the existing host-write permission gate. **P2 (command.json fence) was made a hard gate for P3** — a second reader is only safe after the fence exists.
- **S0 — spawn-time seam, not transport.** `tools/workspace/worker_runtime.py` (~180 lines), `worker_runtime_protocol.py` (~1009 lines: frame codec, `v:1` envelope, per-direction `seq`, `FrameDecodeError`/`FrameEncodeError`/`UnexpectedFrameError`, `ExpectedFence`), `tests/test_worker_runtime_protocol.py` (~987 lines). Design decisions worth keeping: the activation flag is read **at spawn** and never memoised; `ExpectedFence` was **defined locally** on purpose so that `command_channel.py` (filesystem-bound) is **not importable inside the child**.
- **S2 — process transport.** `multiprocessing.get_context("spawn")`, never `fork` (the API server holds live LLM clients, sockets and locks); `socketpair` as the wire; S0's codec moves across intact; `protocol_rejects` counting + escalation threshold belongs to the *supervisor*, not the codec; `WORKER_RUNTIME_MODE` flips to `process` for the worker path only, keeping `thread` green.
- **S4a / S4b — the event seam.** S4a shipped `2ccc1a1d`. The S4b pre-dispatch audit (D1–D4, probe-verified) established: **no single egress** (six families) → the real choke point is `EventBus.publish`; **no protocol widening needed** because `Event` / `Heartbeat` / `WorkerState` frames already existed and were **constructed by nobody** (zero production hits); `FrameEmitter(DIRECTION_FROM_RUNTIME, worker=, instance=, epoch=)` stamps attribution into the **envelope**, so per-worker identity crosses the boundary for free. One child process hosts exactly one worker, so a wildcard subscribe on the child's global bus *is* per-worker attribution. **S4b1 committed `9e3b01de`** (forward child agent events as `event` frames), 57 passed in Docker.
- **S5 — the recorded gap.** Who constructs `WorkerRuntimeHandle` in production? `workspace_routes.py:410` currently always misses. This was explicitly not left silent.
- **S7c — announce-only stale watchdog** (`01ff586e`, phase A; `w8/p3s7c`). New `tools/workspace/worker_stale_watchdog.py` (441 lines) + 8 tests + `worker_manager.py` / `server.py` wiring. The instructive part: a pre-commit fix of a **false safety invariant** — because `global_event_bus` publishes synchronously, "announce-only" was a lie; the watchdog would have terminated in-flight executions of any worker silent >600 s. Closed with a **private `EventBus()` + `stale_callback=None`**, and that privateness is now a documented requirement ("the periodic machinery interrupts without asking").
- **W9 / W10 / W11 — control frames and delivery.** `ProcessWorkerRuntime._route` already dispatches `CancelAck`, `ConfigAck`, `Pong` (early-return style, one `return` per branch — do not let them fall through). W11 covers permission push (§4.4) and the "additive injection" family.

### 5.3 The principle the rewrite is converging on

> **Frames are the only thing that crosses the boundary, and they carry their own attribution.**
> A command is a frame, not a file. Status is an event, not a field someone mutates. A deadline is a protocol fact both ends can see. A brief is state with a version and an ack, not a payload.

Everything in §1–§4 is an instance of the same absence: **no party could tell what another party had already done.** Two clocks, two `command.json` readers, six egress families, 17 status writers, a nudge that is never retracted, a brief that is never acknowledged.

### 5.4 Requirements this history imposes on the design (the reason to keep the document)

1. **One clock.** Soft budget becomes a deadline carried in the protocol; the worker learns that the caller's wait expired, and the caller distinguishes "worker soft-timed-out" from "transport cap". Retire the 4-role timeout split by unification, not by documentation.
2. **One writer per state field.** Status transitions are emitted, not assigned; any remaining writer outside the owner is a bug by definition (cf. `worker_thread.py:3754-3756`).
3. **Report restriction separately from park.** "Blocked" and "idle" are different facts and must not share a status word. This also revives the dead `paused` event path.
4. **Every control action is a frame with an ack.** Kill included: a latch plus a hope is what produces "paused survives kill".
5. **Briefs get a version, an ack, and a restatement point.** Otherwise they will keep being both dropped in transit and eaten by summarisation — and neither will be visible.
6. **Push semantics need a ruling before the design freezes** (lazy vs eager, §4.4) — it decides whether push is load-bearing or an optimisation.
7. **Delete the in-process fast path when the old thread loop goes**, and with it the 5 s bus-attach poll; replace with a "bus created" hook the runtime can register before `start()`.

## 06 — Evidence index

**This document is the synthesis.** Its sources, so the claims can be re-checked rather than trusted:

**Knowledge base (workspace scope)**
- `project/system_architecture.md` — richest. Contains: *Project Historical Trajectory* (Mar–Apr 2026); *Agent Core Inner Mechanics — Verified Deep-Dive (2026-08-01)*; *Pause is reading mode; Stop is turn-scoped* (Wave 7); *P2 — Fenced command channel*; *S4b pre-dispatch audit* (D1–D4); *2026-11-03 — six event egress families*.
- `personal/bugs_and_fixes.md` — 2026-11-09 diagnosed-but-unfixed entries (pause nudge scar; `paused` surviving `kill()`; restriction writes at `worker_thread.py:3754`); the 5 s bus-attach accepted gap.
- `personal/task_tracker.md` — W6/W8 stage records (S2 handoff, S4a/S4b1 commits, S7c), and the W8 S5 ruling.
- `project/status.md` — W7 scope, P3/S0 state, open residuals.
- `project/working_agreements.md` — "briefs must be persisted **and** inlined" (2026-09-19).
- `project/lessons_learned.md`, `project/open_items_register.md` (permission-idle-injection, lazy vs eager), `project/run_design_pass.md` (run_case A6), `personal/agent_worker_timeline.md`, `personal/worker_notes.md`.
- `roadmap` — Phase 3 (3.1 grace turn, 3.2 pause display), F2 multi-agent timeouts, and the 2026-11-09 C13 entry.

**Repo**
- `docs/design-pause-reading-mode.md` (new, Wave 7), `docs/design-worker-lifecycle.md` (contains **4 inline SUPERSEDED blockquotes** + corrections item 15 — read the blockquotes, they are the history), `docs/design-process-isolation.md` (§693-699 states the P2→P3 gate), `docs/design-worker-runtime-process.md` (authority for S2).
- Code as cited inline; treat every `file:line` as dated.

**Commits / branches referenced**
`b9c550ce`, `8ca03339`, `04a4f41d`, `0565aec1` (Wave 7 pause/stop); `9c77577c` (cancel isolation); `2ccc1a1d` (S4a); `9e3b01de` (S4b1); `01ff586e` (S7c phase A); `ada1a469` (`fix/py313-event-bus-leak`); `04ee8988` "W10 T2, briefing" via merge `25840031` (PR #290); `d4abda35` (`bind_pause_event` wiring — refutes the claim that it has no production call site).
Branches: `fix/worker-timeout-and-stale-instance`, `fix/briefing-channel-audit`, `docs/brief-sysprompt-amendments`, `docs/sysprompt-addendum-design`, `docs/sysprompt-implementation-brief`, `w5/addendum-delivery`, `w7-isolation`, `w8/p3*`.

**Known-stale material — do not restate as fact**
- Any claim that a worker thread can be killed in-process (it cannot; WLM only, off by default).
- Any uppercase `CRITICAL` / `TimeState` comparison (production values are lowercase — this bug hid every soft timeout until 2026-08-29).
- `worker_event_schema.json` is stale; `workerInfo.started` is dead code; the pause/resume JSX inside `events.map` in `WorkerOutputPanel` (L786-831) never renders.
- `agent/presenter/event_processor.py:78` `paused` handler is dead code (no emitter since Wave 7).

*Prepared as background for the event-driven worker-control redesign. Synthesis dated 2026-11-09.*
