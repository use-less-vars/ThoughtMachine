# Design: turn lifecycle — the cut, the report, the recovery

**Status:** design document, defect-driven. Branch `fix/forensics-honest-counts`.
**Consistency obligation:** every structural claim here must agree with `.scratch/worker-defects-map.md`
(216 lines). Where the two disagree, the map's MEASURED reading wins and this document is wrong.
**Legend:** `MEASURED` = the code was read and is quoted. `INFERRED` = reasoning from names, docstrings or
adjacent code. `UNMEASURED` = not establishable from this repository. Every claim carries a `path:line`
anchor or one of those two words — the whole point of the exercise is that the plausible sentence was wrong.

---

## 1. Scope: what a turn is, and what is allowed to end it

A **turn** is one query delivered to a worker plus everything the worker does until it answers. The worker's
whole life is one loop: `while not self._stop_event.is_set()` (`tools/workspace/worker_thread.py:4975`), poll
for control input (`:4977`), block on the input queue with a 2 s timeout so the heartbeat can tick
(`:4983-4985`), then dequeue one item and run it. There is exactly one place where a queued query is picked
up, and therefore exactly one place where a turn is refused before it starts: the sentinel / park branch at
`:4989-4996`. MEASURED.

The agent side has three explicit boundaries: a per-query reset plus `_apply_resume_notice` at turn start
(`agent/core/agent.py:1657-1666`), `CHECKPOINT[2]` after the LLM call and before tool dispatch (`:2131-2142`),
and `CHECKPOINT[3]` after the turn with an inline flush (`:2284-2296`). MEASURED.

Five things can end or truncate a turn today:

| # | Ending | Site | Kind |
|---|--------|------|------|
| 1 | the agent answers | `agent/core/agent.py:2284-2296` | normal |
| 2 | token ceiling → tool allow-list collapses | `agent/core/state.py:178-187` | restriction |
| 3 | time ceiling → tool allow-list collapses | `agent/core/state.py:240-252` | restriction |
| 4 | turn cap → tool allow-list collapses | `agent/core/state.py:287-292` | restriction |
| 5 | wall-clock cut by the caller while the worker is mid-turn | `tools/workspace/worker_thread.py:8531-8570`, `tools/workspace/worker_timeout.py:214-285` | out-of-band kill |

Only #5 is a clock. #2–#4 are restrictions: they do not stop the turn, they remove tools from it
(`agent/core/state.py:485-525`). Conflating the two is the origin of the defects in section 2 — a session can
be cut by a restriction or by a caller-side clock, and nothing in the envelope distinguishes them.

---

## 2. Measured defects — the source of truth

Condensed from `.scratch/worker-defects-map.md`; line ranges are the map's own section boundaries.

### S1 — a turn is cut far before the granted timeout (`worker-defects-map.md:8-61`)

- The number the envelope reports is **ceiling arithmetic at the spawn hop only**:
  `TimeBudget.resolve(_requested_timeout, default=WORKER_TIMEOUT_SECONDS, cap=DECLARED_CEILING, strict=True,
  source="worker_thread.spawn")` → `self._dispatch_budget = budget`, `effective_timeout = budget.effective`
  (`tools/workspace/worker_thread.py:7984-7992`). The resolver is `thoughtmachine/timebudget.py:145-190`:
  `None` → default (`:157-167`), above cap → `TimeBudgetExceeded` when strict (`:170-173`) or clamp-to-cap with
  `clamped_by = CEILING_OWNER` when not (`:174-181`), otherwise carried untouched (`:182-190`).
- The layer that actually ends the turn is `agent/core/state.py:87` (`timeout_seconds`) via `update_time_state`
  and `restriction_reason='timeout'` (`:195`, `:240-252`), *or* the token ceiling (`:178-187`), *or* the turn cap
  (`:287-292`). **Nothing joins the two**, and the map's answer to "does the reported number come from the layer
  that cuts?" is **no** (`worker-defects-map.md:52-55`). MEASURED (both ends); INFERRED (that no intermediate
  caller rewrites the number — the map could not find one either).
- The literal `150` named in the observed turn-timeout sentence exists nowhere in `agent/`, `tools/` or
  `tools/workspace/` (`worker-defects-map.md:37-42`), so a 77–188 s cut is consistent with a session-owned soft
  budget or a provider request timeout. **INFERRED: who set 150.** **UNMEASURED: which config key carried it** —
  no session record was available.

### S4 — the token ceiling hard-refuses write tools (`worker-defects-map.md:65-106`)

- CRITICAL flips the restriction with **no grace turn**: `restrictions_active = True`, `restriction_reason =
  'token'` (`agent/core/state.py:178-187`); the only auto-clear requires `token_state != CRITICAL` **and**
  `turn_state == LOW` (`:183-187`). `reason == 'token'` → `['Respond', 'SummarizeTool']` (`:517-518`,
  enforcement `:545-546`); `'timeout'` → additionally `TIMEOUT_WRITE_ESCAPE_TOOLS` (`:519-522`, `:547-553`);
  every other reason → `Respond` only (`:523-525`, `:554-557`). MEASURED.
- The asymmetry is argued, not accidental: the timeout hatch must be the **opposite of a read tool** — write-only,
  `WorkingDocument`, which needs `filesystem:write`, so a session without that permission cannot use the hatch at
  all (`thoughtmachine/timeout_constants.py:33-62`, set at `:63`). The `token` branch has no hatch; its escape is
  `SummarizeTool`. INFERRED (the comment argues the timeout side; nothing argues the token side).
- The refusal is **invisible to the caller**: the executor logs a gate line
  (`agent/core/tool_executor.py:326-332`), records the rejection as an executed tool with `success=False`
  (`:338-341`), and returns the sentence built at `:1125-1140` — but telemetry carries only `elapsed_seconds`,
  `tool_call_count`, `timeout_triggered`, `token_usage` and **no `restriction_reason`**
  (`tools/workspace/worker_thread.py:5476-5481`). MEASURED.

### S2 — after the cut the instance is unusable (`worker-defects-map.md:110-147`)

- `_wait_for_worker_exit` (`tools/workspace/worker_thread.py:8531-8570`) exists to keep `_action_query` from
  returning while the worker is alive (`:8532-8542`) and loops
  `while _join_elapsed < _join_budget: thread.join(timeout=_join_step)` (`:8552-8556`) with **no `ident` guard**.
  In the default runtime mode the supervisor thread is deliberately never started
  (`tools/workspace/worker_runtime.py:1098-1102`), so CPython raises
  `RuntimeError("cannot join thread before it is started")` before the degraded tail that promises "returning
  envelope with thread still running" (`worker_thread.py:8562-8569`). The guard exists 1 100 lines away:
  `if getattr(thread, "ident", None) is None: … skip`, hazard explained in place (`:7414-7423`, applied `:7423`).
  **INFERRED:** that this `RuntimeError` is what the caller experiences as "the instance is unusable".
- Independent second cause — fences are **permanent by design**: the pair-scoped epoch maximum
  (`tools/workspace/worker_registry.py:493-539`) makes `raise StaleWorkerFence("the registry has recorded a newer
  epoch")` (`tools/workspace/worker_runtime.py:1112-1122`) stick forever for an older handle, and epochs are
  retained after unregister on purpose (`worker_registry.py:100-125`). Recovery is `force=True`
  (`worker_thread.py:7390-7400`) or a new worker name. **INFERRED:** no API lowers or clears an epoch.

### S3 — one search call can eat the budget (`worker-defects-map.md:151-176`)

`tools/file_search_tool.py` caps at `MAX_OUTPUT_CHARS = 40_000` (`:22`), `MAX_RESULTS = 200` (`:23`), defaults
`context_lines = 5` (`:30`), `max_results = 50` (`:34`); the assembly loop re-emits path + before/match/after per
hit (`:252-317`) and returns `self._truncate_output(output)` (`:343`). The framework cap is in **tokens** and
optional: `token_limit` defaults to `None` (`tools/base.py:45`), injected only when config carries one
(`agent/core/tool_executor.py:548-549`), estimated `len(text)//4` (`tools/base.py:409-414`) and applied in
`_truncate_output` (`:416-447`). The two caps coincide at ≈10 000 tokens / 40 000 chars, so one call can spend
~12.5 % of the 80 000 critical threshold — the feedback loop into S4 (`worker-defects-map.md:162-172`). MEASURED.

### The S1 ↔ S2 verdict (`worker-defects-map.md:180-199`)

One defect class, **two fixes**: the cut is *unowned at the reporting end* and *unguarded at the recovery end*.
Fixing the clock does not fix the join (`:186-190`), proved by the sibling-guard asymmetry. The summary table is
at `:203-211`; its size classes are the basis for D8 and D9 below.

**The contradiction this document exists to resolve.** The observed episode died at 77–188 s while the envelope
reported `requested_timeout_seconds: 600`, `timeout_clamped: false`, `timeout_source: worker_thread.spawn`.
Those fields describe ceiling arithmetic at one hop (`worker-defects-map.md:52-55`); they are not a statement
about what will cut the turn, and the caller cannot tell. INFERRED: reported number and cutting layer are two
clocks for one deadline, and the observed sentence "turn timeout (150s)" is `agent/core/state.py:240-252`
printing whatever `state.timeout_seconds` held (`:87`), not the caller's granted budget.

---

## 3. Clock inventory — many clocks, one ceiling

| Constant | Value | Site |
|---|---|---|
| `DECLARED_CEILING` | 600 | `thoughtmachine/timebudget.py:65-67` |
| `CEILING_OWNER` (label only) | "agent.config.defaults.HARD_TIMEOUT (…DECLARED_CEILING)" | `thoughtmachine/timebudget.py:69-75` |
| `DEFAULT_REQUEST_SECONDS` | = `SOFT_BUDGET_FALLBACK_SECONDS` | `thoughtmachine/timebudget.py:77-79` |
| `IDLE_TIMEOUT_SECONDS` | 600 | `thoughtmachine/timeout_constants.py:26` |
| `SOFT_BUDGET_FALLBACK_SECONDS` / `SHIPPED_SOFT_BUDGET_SECONDS` | 300 / 300 | `thoughtmachine/timeout_constants.py:28`, `:30` |
| `SPAWN_QUEUE_TIMEOUT` / `WORKER_TIMEOUT_SECONDS` / `HEARTBEAT_STALE_AFTER_S` | 600 each | `agent/config/defaults.py:33`, `:61`, `:43` |
| `QUERY_WAIT_GRACE_SECONDS` | 60 | `agent/config/defaults.py:37` |
| `WORKER_STALE_PROBE/CANCEL/KILL_TIMEOUT_S` | 15 each | `agent/config/defaults.py:101-103` |
| `SOFT_TIMEOUT` / `HARD_TIMEOUT` | 300 / 600 | `agent/config/defaults.py:130-131` |
| `EXEC_KILL_GRACE` | 10 | `agent/config/defaults.py:132` |
| `state.timeout_seconds` / `time_warning_threshold` | `SOFT_BUDGET_FALLBACK_SECONDS` / 240 | `agent/core/state.py:87-88` |
| `JOIN_DEFAULT_WAIT_SECONDS` | 60 | `tools/workspace/worker_timeout.py:34` |

`DECLARED_CEILING` and `HARD_TIMEOUT` are the same number in two files, and `CEILING_OWNER` is a **string**, not
an import (`thoughtmachine/timebudget.py:69-75`) — the "owner" can drift without anything failing. INFERRED:
nothing asserts the two 600s are equal. Resolution semantics are single and well-defined (`:145-190`) and
non-numeric, boolean or non-positive requests degrade to `None` (`:82-94`), so a config typo becomes "use the
default", not an exception. INFERRED: that degradation is what makes the 150 s cut untraceable — a wrong-shaped
value never announces itself.

Trip wires are layered on top: the caller waits `budget.effective + QUERY_WAIT_GRACE_SECONDS`
(`tools/workspace/worker_timeout.py:70-99`, sum at `:99`), logs a clamp (`:97-98`), falls back through
`thread._time_budget` → `thread._timeout_seconds` → default (`:131-164`) and stores it (`:167-172`). But the query
hop re-resolves and **overwrites** the spawn budget (`tools/workspace/worker_thread.py:8755-8762`) before
`deliver_query_to_worker(..., timeout=wait)` (`:8777-8779`). MEASURED: the number the caller waits for is decided
at the query hop, while the number the envelope reports was decided at the spawn hop. That is the two-clock
defect in four lines.

---

## 4. Restriction state machine — who may say "only these tools"

The gate has **one authority and one display copy**, and they are different functions — documented as
"Fail-closed by construction (docs/CONTROL_PLANE.md 6.3 Option B) … `get_allowed_tools()` remains display-only"
(`agent/core/state.py:530`). MEASURED.

- Authority: `is_tool_allowed` (`agent/core/state.py:527-557`) computes inline, defaults to **True** when the
  attribute is missing (`:542`), branches `token` (`:545-546`), `timeout` (`:547-553`), everything else →
  `Respond` only (`:554-557`).
- Display: `get_allowed_tools` (`:485-525`) returns `[]` when unrestricted (`:514-515`) and is consumed to
  *compose prose* (`:14`), not to decide.
- The executor consults only the authority: `if not self.state.is_tool_allowed(tool_name)`
  (`agent/core/tool_executor.py:333`), then the sentence "Tool '<x>' not allowed. Reason: <reason>. Available
  tools: <list>" (`:1125-1140`).

Ownership keeps this safe without a lock: `restrict(reason)` is last-writer-wins with atomic per-attribute
writes and no lock — "Correctness under concurrent use comes from ownership on clear" (`agent/core/state.py:419-455`,
sentence at `:432-436`) — and `clear_restriction(reason=None)` is owner-scoped (`:457-483`): `timeout` LOW clears
only its own reason (`:273-282`), `token` clears only under `token_state != CRITICAL and turn_state == LOW`
(`:183-187`). MEASURED.

Two consequences the code does not enforce. **(a) A write escape hatch can be masked:** `restrict` takes any
reason from any caller, so a session both over time and over tokens keeps whichever reason was written last, and
the `timeout` hatch (`thoughtmachine/timeout_constants.py:63`) survives only if `timeout` won. INFERRED from
`:419-455` (no arbitration) plus the two branch bodies. **(b) Prose can drift from the gate:** the CRITICAL token
warning hard-codes the allow-list as a sentence — "Only Respond and SummarizeTool are available"
(`agent/core/state.py:151`) — while the time branch derives its sentence from `get_allowed_tools()`
(`:240-252`). That hard-coded copy is a second source of truth for one rule. MEASURED.

Naming drift, recorded so nobody "fixes" a call site: methods are `update_token_state` (`:106`),
`update_time_state` (`:195`), `update_turn_state` (`:287`), while their log tags and the map call them `observe_*`
(`[STATE_OBSERVE]` `:192`, `[STATE_OBSERVE_TIME]` `:284`; `worker-defects-map.md:71`). INFERRED: the map's name
is the older one.

---

## 5. Pause — the cooperative path that is deliberately not a cut

Three post-construction signals, each with a different lifetime (`tools/workspace/worker_thread.py:2360-2375`):
`_stop_event` is the terminal kill latch, never cleared (`:2367-2369`); `_park_event` is the park flag driven by
`detach()`/`resume()` (`:2370-2372`); `_turn_cancel` is a per-worker per-turn token, consulted by tools, cleared
at the start of **every** query, bound to the executing tool thread (`:2373-2374`). MEASURED.

- Cancellation is per-thread and cannot be crossed from outside (`tools/cancellation.py:1-17`, "interrupt() cannot
  cross threads" at `:10-13`); the pause event is a **shared reference owned by the caller** (`:58-67`), read via
  `_active_pause_event` (`:70-72`) and `pause_requested()` (`:75-78`); `unbind` drops fallback and pause reference
  and never sets or clears the caller's event (`:43-45`).
- `pause()` delegates to the WLM, sets `_park_event`, marks the pause `manual_only` with a session-scoped record
  (`tools/workspace/worker_thread.py:3036-3054`), calls `agent.enter_reading_mode("pause")` (`:3055-3064`), writes
  a fenced `pause` command and pushes `_PARK_SENTINEL`. It does **not** set `_turn_cancel` — only
  `detach()`/`kill()` interrupt the in-flight tool (`:2867-2871`). MEASURED.
- The park takes effect at the loop's sentinel branch, before the next query is dequeued (`:4989-4996`), and the
  historical reason is preserved in place: the sentinel exists because a leftover bare `None` was read as STOP and
  "broke the loop with work still queued (BUG 1)" (`:89-94`, `:4990-4995`); `_worker_park` bails idempotently on
  three conditions (`:1084-1109`, bails `:1087-1093`). **INFERRED:** a pause requested mid-tool does not truncate
  the current turn; the turn finishes and the *next* query parks.
- Agent side, the contract worth protecting: `enter_reading_mode(reason)` only does `state.restrict(reason)` and
  `_pause_requested = True`, with the cross-thread rule spelled out — "single atomic assignments only … NO Message
  is constructed here" (`agent/core/agent.py:1400-1426`, rule `:1411-1417`); `_apply_pause_reading_mode` runs **on
  the turn thread** and "deliberately does none of the following: no early return, no state transition to
  `PAUSING`, no halt flag, no truncation of the turn" (`:1461-1501`, list `:1476-1479`), consuming the flag at
  `:1497-1501`. `leave_reading_mode` is owner-scoped and arms the resume notice only if the prior reason was
  `pause` (`:1428-1459`); the notice fires once per resume (`:1503-1534`). MEASURED.
- After a pause lands at `CHECKPOINT[2]`, tool calls are still handed to the executor — refused by the allow-list
  rather than dropped (`agent/core/agent.py:2131-2142`) — and the executor winds the batch down once, padding one
  "Not run: the session was paused while an earlier tool was executing." result per remaining call id
  (`agent/core/tool_executor.py:419-435`). MEASURED. That is the pattern to imitate for every other cut:
  **refuse, do not vanish.**

---

## 6. Envelope — what the caller is told, and what it means

- Detection is one predicate whose result is latched: `_worker_timeout_detected(...)` → `self._timeout_triggered`
  (`tools/workspace/worker_thread.py:4655-4678`). The predicate accepts `reason == 'timeout'`, a CRITICAL time
  state **case-insensitively**, or `elapsed >= budget` (`tools/workspace/worker_timeout.py:37-67`); its docstring
  records that the legacy uppercase-only comparison never matched because production values are lowercase
  (`:40-47`). MEASURED.
- A detected timeout overrides the agent: status forced to `'timeout'` even when the agent reported `'progress'`
  (`worker_thread.py:5483-5490`), telemetry built (`:5476-5481`), envelope built (`:5492-5495`).
- Timeout fields are exactly five keys — `requested_timeout_seconds`, `effective_timeout_seconds`,
  `timeout_clamped`, `timeout_clamped_by`, `timeout_source` (`thoughtmachine/timebudget.py:204-224`, keys
  `:218-223`) — under the stated rule "No aliases are emitted. Two names for one number is the same disease as
  two clocks for one deadline" (`:211-213`). MEASURED. The rule is right; the payload still answers the wrong
  question, because `timeout_source: worker_thread.spawn` names the hop that *did the arithmetic*, not the layer
  that cut (`worker-defects-map.md:52-55`).
- Out-of-band cuts emit their own envelope: `_hard_deadline_fallback` stops the worker, joins with
  `join_bound = EXEC_KILL_GRACE`, and returns a terminal envelope (`stopped` or `timeout`) with
  `meta.hard_deadline`, `elapsed_seconds` and cleanup flags `stop_called / join_bounded / exited`
  (`tools/workspace/worker_timeout.py:214-285`). MEASURED.
- In process mode two clocks can race and both orderings must look identical to the caller:
  `tests/test_worker_runtime_process.py:495-522` orders a runtime-reported timeout (delayed outbox so the child's
  frame is already pending, `dispatch_query(..., timeout=0.05)`, asserts `status == "timeout"`,
  `elapsed_seconds > 0.0`, `runtime.lost is False`, and that the next query is still served) against `:525-546`,
  where the local clock wins and the reply is synthesised — docstring: "Both orderings must be indistinguishable
  to the caller". MEASURED. This is the one place where "who cut it" is deliberately unobservable; that is
  acceptable only because the *status* is the same, and it does not license the reporting layer naming the wrong
  owner.

---

## 7. Recovery — fences, joins, and the corpse boundary

- Every lifecycle verb is fenced first: `stop`/`pause`/`resume`/`kill`/`detach` all call `_assert_current` before
  acting (`tools/workspace/worker_runtime.py:1506-1590`, fence `:1532-1537`). The fence message carries session,
  worker name, instance id and both epochs (`:1019-1028`); three proofs must pass — epoch drift, death, successor
  in registry (`:1035-1043`) — with mode-dependent death evidence (`:1045-1060`) and the explicit warning that in
  process mode "asking it `is_alive()` here would report a live worker dead and fence out every route to it"
  (`:1098-1102`). Absence of evidence is never itself a fence: any other exception is logged at DEBUG and the call
  proceeds (`:1062-1068`). MEASURED.
- The registry keeps epochs (`tools/workspace/worker_registry.py:166`, `:173`) and shuts workers down **per
  session**: `shutdown_workers(timeout: float = 5.0, *, session_id)` is keyword-only and rejects `None`
  (`:431-449`), with the process-exit path `ALL_SESSIONS` registered at `:175-178` and documented as the
  process-exit path only (`:432-444`). In process mode a "refusal is an answer and not an exception": verbs send
  control frames with `ACK_TIMEOUT_SECONDS` and return the runtime's `ack.ok`
  (`tools/workspace/worker_runtime_process.py:798-820`). MEASURED.
- In-flight dispatch records are cleared with **exception**, not uniformly: a detach park leaves `'recoverable'`,
  a kill leaves `'cancelled'` (`tools/workspace/worker_thread.py:4683-4695`). MEASURED.
- The join problem is a missing guard, not arithmetic. The safe pattern is written out and applied exactly once
  (`worker_thread.py:7414-7423`: "A thread that was never started in THIS process has no `ident`, and
  `Thread.join()` then raises RuntimeError … skip the wait instead of raising"), with
  `_budget = max(30, thread._timeout_seconds)` and a join retry (`:7432-7435`); the unsafe sibling loops are
  `:8552-8556` and `:9512-9518` (`worker-defects-map.md:126-130`). MEASURED.

**Open UNMEASURED items,** recorded so they are not mistaken for settled: whether a session config key carrying a
soft budget below 300 exists on this branch; which component set `state.timeout_seconds = 150` in the observed
episode; whether any test exercises the `_wait_for_worker_exit` path in process mode (no assert on the guard's
absence was found, and a full test run was not performed for this document).

---

## 8. Decisions

Numbered; each with options, consequences and a recommendation. Sizes refer to `worker-defects-map.md:203-211`.

### D1 — The reported number must come from the layer that cuts (S1)
- **Options:** (a) keep spawn-hop ceiling arithmetic, documented as "declaration only"; (b) carry the resolved
  `TimeBudget` to the agent loop and drive `state.timeout_seconds` from `budget.effective`; (c) drop the timeout
  fields from the envelope.
- **Consequences:** (a) costs nothing now and stays wrong forever — the caller cannot tell a cut from a grant
  (`worker-defects-map.md:52-55`); (b) one clock, but the query hop must stop overwriting the spawn budget
  (`worker_thread.py:8755-8762`) and the agent must accept a per-turn budget; (c) loses the only forensic numbers.
- **Recommendation:** (b) — architectural, and the map already prices it as such.

### D2 — One ceiling, not two identical 600s
- **Options:** (a) leave `DECLARED_CEILING = 600` and `HARD_TIMEOUT = 600` independent; (b) import one from the
  other; (c) assert equality in a test.
- **Consequences:** (a) `CEILING_OWNER` is a string (`thoughtmachine/timebudget.py:69-75`), so drift is silent and
  a clamp would name the wrong owner; (b) couples `thoughtmachine` to `agent.config`, inverting the dependency the
  constants module exists to avoid (`thoughtmachine/timeout_constants.py:56-62`); (c) keeps both literals and makes
  drift a red test.
- **Recommendation:** (c) — the constants file must stay dependency-free by contract.

### D3 — No anonymous timeouts
- **Options:** (a) leave config as-is; (b) require every timeout to reach the envelope with a `timeout_source`
  naming the config key that set it, rejecting unlabelled values; (c) log-only.
- **Consequences:** (a) the 150 s episode stays unexplainable (INFERRED: unlabelled session budget); (b) small
  patch in `TimeBudget.resolve` plus one validation point, and it makes the `_as_requested` degradation
  (`timebudget.py:82-94`) visible instead of silent; (c) cheapest, and nobody reads logs mid-incident.
- **Recommendation:** (b), keeping (c)'s log line.

### D4 — Arbitration between restriction reasons
- **Options:** (a) keep last-writer-wins (`agent/core/state.py:419-455`); (b) fixed precedence order with the
  winner recorded; (c) a reason *set* with a derived effective restriction.
- **Consequences:** (a) a token-critical session can mask an available write hatch and the caller sees one reason
  (INFERRED); (b) small change, keeps one `restriction_reason` field and one sentence, needs a stated order;
  (c) most honest, largest blast radius — both `get_allowed_tools` and `is_tool_allowed` branch on a scalar
  (`:485-525`, `:527-557`).
- **Recommendation:** (b) — precedence `token > timeout > turn > pause` (most context-destroying first), one line,
  and revisit (c) only if a second consumer appears.

### D5 — Keep the token restriction, fix its invisibility (S4)
- **Options:** (a) give `token` a write escape hatch like `timeout`; (b) keep the asymmetry and surface the reason
  in the envelope; (c) keep it and do nothing.
- **Consequences:** (a) contradicts the rationale that a restriction is resource management and the hatch must be
  write-only (`thoughtmachine/timeout_constants.py:33-62`) — for `token` the equivalent action is `SummarizeTool`,
  already allowed (`agent/core/state.py:517-518`); (b) one telemetry key in `worker_thread.py:5476-5481` plus the
  existing rejection sentence (`tool_executor.py:1125-1140`); (c) reproduces exactly the incident in which the
  refusal was invisible.
- **Recommendation:** (b) — add `restriction_reason` to telemetry; do not add a hatch.

### D6 — Prose must be derived, never copied
- **Options:** (a) leave "Only Respond and SummarizeTool are available" hard-coded (`agent/core/state.py:151`);
  (b) derive it from `get_allowed_tools()` as the time branch does (`:240-252`); (c) test that the two agree.
- **Consequences:** (a) a second source of truth for one rule — the exact shape of three of the five defects
  (`worker-defects-map.md:213-216`); (b) small patch, removes the drift class; (c) leaves the copy and traps it.
- **Recommendation:** (b), plus (c) as the regression net.

### D7 — Pause stays cooperative; do not make it a cut
- **Options:** (a) status quo: `pause()` does not set `_turn_cancel`, the in-flight tool finishes and the park
  happens at the next query (`worker_thread.py:3019-3074`, `:4989-4996`); (b) make `pause()` set `_turn_cancel`
  like `detach()` (`:2867-2871`).
- **Consequences:** (a) a long tool delays the pause — the documented contract, since the reading-mode path
  "deliberately does none of the following: no early return … no truncation of the turn"
  (`agent/core/agent.py:1476-1479`); (b) pause becomes a cut, and the resume notice (`:1503-1534`) starts firing
  on truncated turns, which is the indistinguishability the process-mode tests exist to prevent
  (`tests/test_worker_runtime_process.py:495-546`).
- **Recommendation:** (a), with the boundary made explicit in the verb's docstring so it is not "fixed" later.

### D8 — One join guard, three call sites (S2.a)
- **Options:** (a) copy the guard into `_wait_for_worker_exit`; (b) hoist `getattr(thread, "ident", None) is None`
  into one helper used at `:7414-7423`, `:8552-8556`, `:9512-9518`; (c) make the supervisor thread always
  startable so `join()` is legal.
- **Consequences:** (a) fixes one of three and re-creates the asymmetry that produced the bug
  (`worker-defects-map.md:186-190`); (b) small patch (<10 lines per the map), one behaviour stated once;
  (c) removes the guard's reason to exist but changes process-mode semantics
  (`tools/workspace/worker_runtime.py:1098-1102` says the thread is not started on purpose).
- **Recommendation:** (b).

### D9 — Epoch fences stay monotone; make the way out discoverable (S2.b)
- **Options:** (a) add a fence TTL or clear an epoch when a predecessor unregisters; (b) keep monotonicity and
  document `force=True` as the recovery; (c) keep monotonicity and add an explicit `recover` verb.
- **Consequences:** (a) breaks the safety property the pair-scoped maximum exists for — a successor reusing
  instance 1 while the predecessor still holds a handle (`worker_registry.py:494-505`); (b) zero code risk, but
  the fence error must name the recovery or operators keep calling a fenced instance "unusable"; (c) new surface
  with (b)'s blast radius plus a verb to maintain.
- **Recommendation:** (b) — a policy call, not a patch, and recorded as such.

### D10 — One output budget, counted in one unit (S3)
- **Options:** (a) leave the char cap and the optional token cap (`tools/base.py:45`, `file_search_tool.py:22`);
  (b) dedupe overlapping per-file windows in `FileSearchTool` and add a per-file budget beside the global one;
  (c) always inject a token limit.
- **Consequences:** (a) a 40 000-char result is ≈12.5 % of the critical threshold
  (`worker-defects-map.md:162-172`); (b) small patch (<30 lines), a diet fix — nothing counts output tokens except
  `len(text)//4` (`tools/base.py:409-414`); (c) changes every tool at once and still leaves the estimator
  approximate.
- **Recommendation:** (b) now, (c) only together with a real counter.

### D11 — Record the cut, not just the status
- **Options:** (a) leave telemetry as `elapsed_seconds / tool_call_count / timeout_triggered / token_usage`
  (`worker_thread.py:5476-5481`); (b) add `restriction_reason` and the clamp triple (`timeout_clamped`,
  `timeout_clamped_by`, `timeout_source`); (c) add a free-text note.
- **Consequences:** (a) postmortems keep reconstructing the owner by hand and the S1/S2 episode stays
  unreproducible; (b) additive, reuses fields that already exist (`timebudget.py:204-224`), no alias problem;
  (c) unparseable.
- **Recommendation:** (b) — the cheap half of D1, worth doing even if D1 slips.
