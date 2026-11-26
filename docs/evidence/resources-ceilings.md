# Ceilings inventory — declared / owned / read / enforced / logged

Read-only inventory. Every claim is labelled **MEASURED** (code read, anchored at `path:line`),
**INFERRED** (reasoned from name/docstring only) or **UNMEASURED** (not establishable from this repo).
No fixes, no refactors. Repo root `/home/jojo/PycharmProjects/ThoughtMachine-dev`.

Legend for the five questions: (1) DECLARED, (2) OWNER, (3) READ, (4) ENFORCED, (5) LOGGED.

---

## A. Wall-clock ceilings

### A1 — `DECLARED_CEILING` = 600 s (the one hard ceiling)

| Q | Answer |
|---|---|
| 1 DECLARED | `DECLARED_CEILING = 600` — `thoughtmachine/timebudget.py:67` **MEASURED** |
| 2 OWNER | Module constant of `thoughtmachine.timebudget` (a real import target, `thoughtmachine/timebudget.py:67`). The *prose* owner is a **STRING**: `CEILING_OWNER = "agent.config.defaults.HARD_TIMEOUT (thoughtmachine.timebudget.DECLARED_CEILING)"` — `thoughtmachine/timebudget.py:72-75` **MEASURED**. It names `agent/config/defaults.py` but is not derived from it (no import of `defaults` in timebudget.py; the module imports only `numbers`, `dataclasses`, `typing`, `thoughtmachine.timeout_constants`, `timebudget.py:59-63` **MEASURED**) → the string can drift from the constant it names; nothing in the repo compares them. |
| 3 READ | `tools/workspace/worker_thread.py:6677-6682` (dispatch, `strict=True`), `:8014-8019` (`_action_spawn`, `strict=True`), `tools/workspace/worker_timeout.py:78-108` (`_worker_query_wait_timeout`), `tools/workspace/worker_timeout.py:112-135` (`clamped_wait_for_job_timeout`), `tools/workspace/worker_thread.py:9155-9163` (join). **MEASURED**. The hop that reports IS the hop that resolves: `worker_thread.py:6721-6722` spreads `self._dispatch_budget.as_envelope_fields()` into the returned envelope dict. |
| 4 ENFORCED | Two behaviours, both in `TimeBudget.resolve`: `strict=True` raises `TimeBudgetExceeded` (`thoughtmachine/timebudget.py:167-170`) → dispatch returns a refusal envelope, no spawn/queue (`tools/workspace/worker_thread.py:6684-6699`); `strict=False` clamps `effective = cap` and sets `clamped=True` (`thoughtmachine/timebudget.py:171-179`) → caller logs it (`tools/workspace/worker_timeout.py:107-108`, `:130-134`, `tools/workspace/worker_thread.py:9161-9162`). **MEASURED**. Nothing kills a thread at 600 s — see A2/F. |
| 5 LOGGED | Logger `tools.workspace.worker_timeout` (`worker_timeout.py:29`): `logger.warning("query wait: %s", …)` `:107`, `logger.warning("wait_for_job: %s", …)` `:130`. Structured keys on every timed envelope: `requested_timeout_seconds`, `effective_timeout_seconds`, `timeout_clamped`, `timeout_clamped_by`, `timeout_source` (`thoughtmachine/timebudget.py:206-216`, spread at `worker_thread.py:6722`). Refusal envelope adds `declared_ceiling_seconds`, `timeout_ceiling_owner`, `status="rejected"` (`worker_thread.py:6689-6696`). **MEASURED** |
| malformed / missing / zero / negative / non-numeric | `_as_requested` (`thoughtmachine/timebudget.py:82-94`) returns `None` for `None`, `bool`, any non-`numbers.Real`, and any `<= 0`; `resolve` then takes the caller's `default` with `requested=None`, `clamped=False` (`:156-166`) — i.e. **absent, not refused, and no log line**. A **string** `"420"` is non-`Real` → treated as absent → default silently (same lines). **MEASURED** |
| reported-vs-cutting | Reported number (`effective_timeout_seconds`) comes from the **same** object (`TimeBudget`) that the waiting hop uses for its deadline (`worker_thread.py:9163` `total = int(budget.effective)`; `worker_timeout.py:100-105` `float(budget.effective) + QUERY_WAIT_GRACE_SECONDS`). The layer that actually CUTS the wait is the caller's deadline loop (`worker_thread.py:9164` `deadline = time.monotonic() + total`; `infra/workspace_lifecycle_manager.py:420-428`), **not** the agent loop. **MEASURED** |

**Three different defaults for one action (MEASURED):** dispatch resolves with `default=DEFAULT_REQUEST_SECONDS` = 300 (`worker_thread.py:6679`), `_action_spawn` re-resolves with `default=WORKER_TIMEOUT_SECONDS` = 600 (`worker_thread.py:8013-8019`), `_action_query` uses `default=DEFAULT_REQUEST_SECONDS` (`worker_thread.py:8793-8798`), `_action_join` uses `default=JOIN_DEFAULT_WAIT_SECONDS` = 60 (`worker_thread.py:9155-9158`, `tools/workspace/worker_timeout.py:35`). A caller that omits `timeout_seconds` therefore gets a *different* number per action, and the envelope reports whichever one that hop resolved.

### A2 — `IDLE_TIMEOUT_SECONDS` = 600 s

| Q | Answer |
|---|---|
| 1 DECLARED | `IDLE_TIMEOUT_SECONDS = 600` — `thoughtmachine/timeout_constants.py:26` **MEASURED** (re-exported at `agent/config/defaults.py:25-29` **MEASURED**) |
| 2 OWNER | Module constant of the dependency-free leaf `thoughtmachine.timeout_constants` (real import). **MEASURED** |
| 3 READ | `infra/containers/executor.py:26` (import), `:657` (`idle_timeout: int = IDLE_TIMEOUT_SECONDS` default), `:668` (stored), `:1091` (`if self.container and (time.time() - self.last_used) > self.idle_timeout: self.close()`). Also `thoughtmachine/security.py:673` imports it as the def-time default of `setup_docker_sandbox(..., idle_timeout=IDLE_TIMEOUT_SECONDS)` (`security.py:683`, passed on `:722`). **MEASURED**. The reading hop is *not* a reporting hop: no number is returned to any caller. |
| 4 ENFORCED | `infra/containers/executor.py:1091-1092` — closes (destroys) the pooled container on the **next** `execute()` call; it is a **lazy, check-on-use** idle policy, not a timer. Nothing runs at 600 s of silence by itself. **MEASURED** |
| 5 LOGGED | NONE at the idle check (`executor.py:1091-1092` has no log call). `close()` logs through the container-lifecycle audit (`emit_destroy_audit`, `executor.py:1156-1160`). **MEASURED** |
| malformed / zero / negative | `idle_timeout=0` is used deliberately for ephemeral builds (`infra/containers/executor.py:1483`): `(time.time()-last_used) > 0` is true on every call → container closed before each execute. Negative would behave the same. No validation, no error path. **MEASURED** |
| reported-vs-cutting | Nothing reported. Cutting layer = `DockerExecutor.execute` (`infra/containers/executor.py:1091`). **MEASURED** |

### A3 — `SOFT_BUDGET_FALLBACK_SECONDS` = 300 s and `SHIPPED_SOFT_BUDGET_SECONDS` = 300 s

| Q | Answer |
|---|---|
| 1 DECLARED | `SOFT_BUDGET_FALLBACK_SECONDS = 300` — `thoughtmachine/timeout_constants.py:28`; `SHIPPED_SOFT_BUDGET_SECONDS = 300` — `thoughtmachine/timeout_constants.py:30`. Both re-exported at `agent/config/defaults.py:25-29`, and `models.py:13` **MEASURED** |
| 2 OWNER | Module constants of `thoughtmachine.timeout_constants`. The *shipped config key* is the **STRING** `"agent_soft_budget_seconds"` in `resources/default_config.json` (`SHIPPED_SOFT_BUDGET_SECONDS` documents that key, `timeout_constants.py:9-10` **MEASURED**; string consumed at `agent/config/models.py:314` `values.pop('agent_soft_budget_seconds', None)`) |
| 3 READ | `agent/core/state.py:8-11` + `:102` (`timeout_seconds: int = SOFT_BUDGET_FALLBACK_SECONDS`); `agent/config/models.py:223` (`timeout_seconds: int = Field(default=SOFT_BUDGET_FALLBACK_SECONDS, …)`); `agent/config/models.py:314-316` (a supplied `agent_soft_budget_seconds` becomes `timeout_seconds` when no explicit value is given); `thoughtmachine/timebudget.py:79` (`DEFAULT_REQUEST_SECONDS = SOFT_BUDGET_FALLBACK_SECONDS`). **MEASURED**. `SHIPPED_SOFT_BUDGET_SECONDS` is imported but I found **no** non-test read of it (`agent/config/defaults.py:28` re-export only) → **INFERRED** (used by tests/docs, not by runtime code). |
| 4 ENFORCED | For the agent loop: **NONE** — `state.update_time_state` only warns and (at ≥ timeout) activates tool restrictions; `agent/core/agent.py:1743-1745` states the intent explicitly: *"If time is critical, log it but don't stop — soft restriction"*, and the loop's stop comes from `max_turns`, not from the clock (`agent.py:1695` `for turn in range(self.config.max_turns)`). **MEASURED**. It becomes a real deadline only indirectly, when it is handed to `TimeBudget.resolve` as a *request* by the worker hop (`worker_timeout.py:78-108`) or as the LQM wait bound (`infra/workspace_lifecycle_manager.py:420`). |
| 5 LOGGED | `state.py` fires a `time_warning` event with `logger.log_time_warning(old, new, elapsed_seconds, warning)` (`agent/core/state.py:281`) and `log('DEBUG','pipeline.warning', …)` (`state.py:251`, `:283`). No field carries the *configured* timeout in the event data (`state.py:284-290` carries `elapsed_seconds`, `new_state`, `warning_message`, `state`); the timeout only appears inside the rendered prose (`state.py:255-257`) and in the DEBUG line at `:304`. **MEASURED** |
| malformed / zero / negative | No validation in `state.py`: `timeout = getattr(self.config,'timeout_seconds', self.timeout_seconds)` (`state.py:228`) is used raw. `timeout=0` → every elapsed value is `>= 0` → immediately CRITICAL + restrictions (`state.py:238-243`). Negative → same. Non-numeric → `TypeError` at the comparison `:238` (no guard). **MEASURED** (code path read; the exception type is the plain Python consequence of comparing `float < str`). |
| reported-vs-cutting | The number the model SEES (warning prose, `state.py:255-257`) is read from `config.timeout_seconds`, i.e. the *declared* budget; the thing that CUTS work is a different layer (caller deadline `worker_thread.py:9164` / LQM `workspace_lifecycle_manager.py:420-428` / `max_turns`). **MEASURED** → gap recorded in F. |

### A4 — `SOFT_TIMEOUT` = 300 s, `HARD_TIMEOUT` = 600 s, `EXEC_KILL_GRACE` = 10 s

| Q | Answer |
|---|---|
| 1 DECLARED | `SOFT_TIMEOUT = 300` — `agent/config/defaults.py:130`; `HARD_TIMEOUT = 600` — `agent/config/defaults.py:131`; `EXEC_KILL_GRACE = 10` — `agent/config/defaults.py:132` **MEASURED** |
| 2 OWNER | Module constants of `agent.config.defaults`, *commented* as belonging to `infra/workspace_lifecycle_manager.py` (`defaults.py:128`). **MEASURED**. Note the timebudget docstring claims `HARD_TIMEOUT` is "re-exported from here" (`thoughtmachine/timebudget.py:41`) but `defaults.py` does **not** import from `timebudget`, and `timebudget` does not import `defaults` — the two 600s are independent literals. **MEASURED** (see E). |
| 3 READ | `infra/workspace_lifecycle_manager.py:75` (import), `:420` (`wait_bound = timeout if timeout is not None else SOFT_TIMEOUT`), `:382` (docstring), `:249-250` (soft-timeout warning state). `HARD_TIMEOUT` is imported at `:75` but I found no runtime read of it in `infra/workspace_lifecycle_manager.py` → **INFERRED** unused-by-that-module. `EXEC_KILL_GRACE` likewise imported at `:75`, no other occurrence under `infra/` (search) → **INFERRED** unused under `infra/`. **MEASURED** for the import lines. |
| 4 ENFORCED | `infra/workspace_lifecycle_manager.py:420-428`: deadline loop, on expiry `self.transition_timeout()` then `raise TimeoutError(f"query {query_id} timed out after {wait_bound}s")`. This IS a real cut (of the query wait). **MEASURED**. `HARD_TIMEOUT`/`EXEC_KILL_GRACE`: **NONE** found (see F). |
| 5 LOGGED | `_log("WARNING", "workspace.lifecycle", …)` for the no-handler case (`:416-418`); the timeout path raises rather than logs; soft-timeout warning emitted via `_emit_soft_timeout_warning(query_id)` (`:429-430`). The raised message carries `wait_bound` (`:427`). **MEASURED** |
| malformed / zero / negative | `timeout` param: `if timeout is not None` (`:420`) — a `0` is honoured → `remaining <= 0` on the first pass → immediate `TIMED_OUT` + `TimeoutError` (`:423-428`). Non-numeric → `TypeError` at `deadline = time.monotonic() + wait_bound` (`:421`). No config parse path. **MEASURED** |
| reported-vs-cutting | Same hop: the number in the `TimeoutError` message (`:427`) is the number the loop cut on. **MEASURED** |

### A5 — `WORKER_TIMEOUT_SECONDS` = 600 s

| Q | Answer |
|---|---|
| 1 DECLARED | `WORKER_TIMEOUT_SECONDS = 600` — `agent/config/defaults.py:61` **MEASURED** |
| 2 OWNER | Module constant of `agent.config.defaults`; consumed as the terminal fallback of a four-hop resolution chain (config **keys** are strings: `"worker_timeout_seconds"` / `"timeout_seconds"`). **MEASURED** |
| 3 READ | Chain, in order: constructor param > worker definition `"timeout_seconds"` > `agent_config["worker_timeout_seconds"]` > `session_config["worker_timeout_seconds"]` > module default — `tools/workspace/worker_thread.py:2222-2239` (import at `:80`). Second read: `worker_thread.py:8013-8019` (`_action_spawn` re-resolve). Third: `tools/workspace/worker_timeout.py:24` (import; not read again in that file after the default was replaced by `DEFAULT_REQUEST_SECONDS` — `worker_timeout.py:78-81` uses `fallback=DEFAULT_REQUEST_SECONDS`). **MEASURED**. |
| 4 ENFORCED | Via A1: `TimeBudget.resolve(..., strict=True)` at `worker_thread.py:8013-8019` (refuses > 600) and the deadline at `worker_thread.py:9164` for join; the worker's own agent loop does **not** stop on the clock (A3/F). **MEASURED** |
| 5 LOGGED | No dedicated log; the resolved value is reported structurally through the envelope keys (A1 row 5) and as `effective_timeout` handed to the thread (`worker_thread.py:8020`). **MEASURED** |
| malformed / missing / zero / negative / non-numeric | Each hop checks `is None` explicitly (`worker_thread.py:2228-2234`), so a JSON `null` falls through to the next hop. A **string** value passes `is not None` and is then rejected by `_as_requested` (non-`Real`) → treated as absent at the TimeBudget hop, i.e. silently replaced by 300/600 depending on hop. Zero/negative: `AccountConfig.worker_timeout_seconds` has `ge=1` (`agent/config/models.py:176-179`) and `SessionConfig` the same (`agent/config/session_config.py:220-223`), so pydantic refuses it *if* it arrives through those models; the raw `worker_thread` chain has no check. **MEASURED** for the `ge=1` and `is None` lines; **INFERRED** for "pydantic refuses" (field constraint semantics). |
| reported-vs-cutting | Reported (`effective_timeout_seconds`) = the value the caller deadline uses; the *worker's* own soft timeout is a different mechanism (state.py warning only). **MEASURED** |

### A6 — `SPAWN_QUEUE_TIMEOUT` = 600 s, `QUERY_WAIT_GRACE_SECONDS` = 60 s

| Q | Answer |
|---|---|
| 1 DECLARED | `SPAWN_QUEUE_TIMEOUT = 600` — `agent/config/defaults.py:33`; `QUERY_WAIT_GRACE_SECONDS = 60` — `agent/config/defaults.py:37` **MEASURED** |
| 2 OWNER | Module constants of `agent.config.defaults`. **MEASURED** |
| 3 READ | `QUERY_WAIT_GRACE_SECONDS`: `tools/workspace/worker_timeout.py:24` (import) and `:105` (`return float(budget.effective) + QUERY_WAIT_GRACE_SECONDS`) — the only runtime read I found. `SPAWN_QUEUE_TIMEOUT`: **no** read found in `tools/workspace/*.py` (search for the name returns only the declaration) → **INFERRED** (dead or read from a path outside the searched trees). **MEASURED** for the declaration + the single `QUERY_WAIT_GRACE_SECONDS` read. |
| 4 ENFORCED | `QUERY_WAIT_GRACE_SECONDS` widens the caller's wait (`worker_timeout.py:105`), which the caller then enforces at `worker_thread.py:9164`/`:8793-8798`. `SPAWN_QUEUE_TIMEOUT`: **NONE** found. **MEASURED** |
| 5 LOGGED | NONE for either value by name; the grace is only visible inside the effective wait. **MEASURED** |
| malformed | Constants, not config; no parse path. Grace is added with `+` to a float — a non-numeric override raises `TypeError` at `worker_timeout.py:105`. **MEASURED** |
| reported-vs-cutting | The grace is *included* in the reported number (`effective` + grace at `:105`), but the envelope field `effective_timeout_seconds` comes from the earlier `TimeBudget` and therefore **excludes** the grace (`worker_thread.py:6722` spreads the dispatch budget). **MEASURED** → gap in F. |

### A7 — `WORKER_STALE_*` ladder (PROBE 15 / CANCEL 15 / KILL 15 / ANNOUNCE 90 / WATCHDOG 30)

| Q | Answer |
|---|---|
| 1 DECLARED | `WORKER_STALE_PROBE_TIMEOUT_S = 15` — `agent/config/defaults.py:101`; `WORKER_STALE_CANCEL_TIMEOUT_S = 15` — `:102`; `WORKER_STALE_KILL_TIMEOUT_S = 15` — `:103`; `WORKER_STALE_ANNOUNCE_AFTER_S = 3 * HEARTBEAT_INTERVAL_S` (= 90) — `:96`; `WORKER_STALE_WATCHDOG_INTERVAL_S = 30` — `:90`; `HEARTBEAT_STALE_AFTER_S = 600` — `:43`. **MEASURED** |
| 2 OWNER | Module constants of `agent.config.defaults`; the comment claims they are read by `worker_stale_watchdog.WorkerStaleWatchdog` as per-instance defaults (`defaults.py:97-100`). **MEASURED** (claim), read verified below. |
| 3 READ | `tools/workspace/worker_stale_watchdog.py` — **UNMEASURED in this pass** (file not read; the only anchor I have is the comment `agent/config/defaults.py:97-100`). `HEARTBEAT_STALE_AFTER_S` is imported by `tools/workspace/worker_thread.py:75-77`. **MEASURED** for the import. |
| 4 ENFORCED | The watchdog is documented **ANNOUNCE-ONLY** — "never killed, cancelled or interrupted -- phase A is announcement only" (`agent/config/defaults.py:91-95`) → **INFERRED**: the 15 s ladder budgets do not cut anything today. |
| 5 LOGGED | **UNMEASURED** (watchdog file not read). |
| malformed | Constants; no parse path. **MEASURED** |
| reported-vs-cutting | Named for cutting, documented as announce-only → recorded in F. |

### A8 — `agent/core/state.py` `timeout_seconds` / `time_warning_threshold`

| Q | Answer |
|---|---|
| 1 DECLARED | `timeout_seconds: int = SOFT_BUDGET_FALLBACK_SECONDS` — `agent/core/state.py:102`; `time_warning_threshold: int = 240` — `agent/core/state.py:103`. Mirrored in config: `agent/config/models.py:223-225` (`timeout_seconds` default 300, `time_warning_threshold` default 240). **MEASURED** |
| 2 OWNER | Dataclass field default on the state object (`agent/core/state.py:102-103`) — a *placeholder*: the live value is always taken from `config` (`state.py:228-229` `getattr(self.config, 'timeout_seconds', self.timeout_seconds)`). **MEASURED** |
| 3 READ | `state.py:228-229` in `update_time_state`; called from `agent/core/agent.py:1728-1730` (`elapsed = time.time() - self.state.time_start`; `self.state.update_time_state(elapsed)`). **MEASURED**. The reporting hop is `agent.py:1731-1742` (it turns the event into a `[SYSTEM NOTIFICATION]` message in the conversation). |
| 4 ENFORCED | **No cut.** `state.py:232-243` computes LOW/WARNING/CRITICAL; at CRITICAL it sets `restrictions_active`/`restriction_reason='timeout'` (via the block at `:259-272`), i.e. tool allow-list narrowing, and `agent.py:1743-1745` explicitly does not stop. The *detection* of that state is later reused as a timeout signal for the worker (`tools/workspace/worker_timeout.py:38-52` → `worker_thread.py:4697-4705`). **MEASURED** |
| 5 LOGGED | `logger.log_time_warning(old, new, elapsed_seconds, warning)` (`state.py:281`), `log('DEBUG','pipeline.warning', …)` (`:251`, `:283`), `log('DEBUG','[STATE_OBSERVE_TIME]', …)` (`:304`). Event fields: `old_state`, `new_state`, `elapsed_seconds`, `warning_message`, `state` (`:284-290`). **MEASURED** |
| malformed / zero / negative / non-numeric | `warning_at >= timeout` (`state.py:233`) is a defensive branch: if the threshold is mis-set at/above the timeout, WARNING is skipped and CRITICAL fires at `elapsed >= timeout`. Zero/negative timeout → CRITICAL from the first turn. Non-numeric → `TypeError` at the `>=` comparison (`:233`) — no guard. `None` → `TypeError` in `getattr` fallback? No: `getattr` returns the attribute even if `None`, so `None >= None` raises `TypeError` at `:233`. **MEASURED** (path read; the TypeError is Python's, not an explicit raise). |
| reported-vs-cutting | Reported number = `config.timeout_seconds` (A3 row 3/5); the cutting layer is **not** this one — the agent loop does not stop on the clock. **MEASURED** → F. |

---

## B. Token / context ceilings

### B1 — `token_monitor_warning_threshold` = 65000 / `token_monitor_critical_threshold` = 80000

| Q | Answer |
|---|---|
| 1 DECLARED | `token_monitor_warning_threshold: int = Field(default=65000, ...)` — `agent/config/models.py:189`; `token_monitor_critical_threshold: int = Field(default=80000, ...)` — `agent/config/models.py:190` **MEASURED** |
| 2 OWNER | `AgentConfig` (pydantic) fields, marked HOT_SWAPPABLE in the hot-swap map (`agent/config/models.py:101-102`) **MEASURED** |
| 3 READ | `agent/core/state.py:126-131` — `update_token_state` compares `total_tokens` against `config.token_monitor_warning_threshold` / `..._critical_threshold`; called from the agent's token update path (`agent/core/agent.py:1741-1742`) **MEASURED**. Also passed to tools as `agent_config['token_monitor_*_threshold']` (`agent/core/tool_executor.py:771-784`) **MEASURED**. Reporting hop: `tools/workspace/check_system.py:1420-1421` (`token_limits["context_limit"]`) reports a *different* number (the capability `max_context_length`, see B3) **MEASURED** |
| 4 ENFORCED | Soft only: WARNING fires an event (`state.py:168-172`), CRITICAL sets `restrictions_active=True` / `restriction_reason='token'` (`state.py:186-192`) — i.e. tool allow-list narrowing. No message is dropped, no LLM call is refused, no number is enforced at the token boundary. **MEASURED** |
| 5 LOGGED | `state.py:168-172` emits the warning event; recovery event at `state.py:174-185` **MEASURED**. The *configured thresholds* are not carried in the event payload (the event carries the measured total) → **INFERRED** from the event construction at `state.py:168-192` |
| malformed / zero / negative / non-numeric | No validation in `state.py`: raw `config.*` reads at `:126-131`. `critical <= warning` → CRITICAL is reached without a WARNING stage; `0`/negative → CRITICAL on first observation. Non-numeric → `TypeError` at the comparison (plain Python consequence, no explicit guard) **INFERRED** (path read, exception type not exercised) |
| reported-vs-cutting | The model is *told* about tokens through the warning prose built at `state.py:168-192`; nothing in this path cuts. The real cut, when it happens, is the **provider's** error mapped to `token_limit_exceeded` (`agent/core/llm_client.py:398-408`) and handled in B4. **MEASURED** |

### B2 — `tool_output_token_limit` = 10000 (the per-tool-result ceiling)

| Q | Answer |
|---|---|
| 1 DECLARED | `tool_output_token_limit: int = Field(default=10000, ...)` — `agent/config/models.py:212` **MEASURED**. Legacy key `tool_output_limit` is renamed onto it (`agent/config/loader.py:263-266`) **MEASURED** |
| 2 OWNER | `AgentConfig` field, HOT_SWAPPABLE (`agent/config/models.py:124`) **MEASURED** |
| 3 READ | Injected into every tool instance as the `token_limit` field: `agent/core/tool_executor.py:548-549`; declared INFRA-side and hidden from the model: `tool_executor.py:470`, `:532`; the tool's own field default is `None` (`tools/base.py:53`) **MEASURED**. Name variance worth noting: the `agent_config` dict handed to tools uses `token_output_token_limit` (word order swapped) — `tool_executor.py:771-784` **MEASURED** |
| 4 ENFORCED | `tools/base.py:457-488` `_truncate_output`: `limit is None or limit <= 0` → **no truncation at all**; otherwise `target_chars = limit * 4`, cut on the last newline past 80 % of target, else a `-100`-char loop. Opt-out flag `skip_output_truncation` (`tools/base.py:149-151`) **MEASURED** |
| 5 LOGGED | Announced **in the tool result text**, not in a log call: `"\n... (output truncated to {limit} tokens, original was {estimated_tokens} tokens)"` (`tools/base.py:485-488`) **MEASURED**. The `estimated_tokens` in that notice comes from `len(text)//4` (`tools/base.py:452-455`, comment: *"Original tiktoken implementation disabled due to network issues in Docker"*) — so the announced numbers are character/4 estimates, not tokenizer counts **MEASURED** |
| malformed / zero / negative | `limit <= 0` → truncation silently **disabled** (`tools/base.py:457-461`); `limit = 0` is therefore not "no output" but "unlimited output". Non-numeric → `TypeError` at `limit * 4` **INFERRED** |
| reported-vs-cutting | Same object, same call: the cut and the announced limit come from one `limit` value (`tools/base.py:457-488`). The *unit* differs from the monitor in B1 (chars/4 here, tiktoken/fallback there) → gap in F. **MEASURED** |

### B3 — context window (the ceiling that is *reported* but never *checked*)

| Q | Answer |
|---|---|
| 1 DECLARED | Model→window table inside `TokenCounter.get_model_context_window` — `agent/core/token_counter.py:150-171` (dict at `:158`: gpt-4 8192, gpt-4-32k 32768, gpt-4-turbo/gpt-4o 128000, gpt-3.5-turbo 16385, deepseek* 128000, step-3.5 128000, claude-3-* 200000, default 128000) **MEASURED**. A **second, hand-maintained copy** of the same table lives in `tools/workspace/worker_thread.py:2481-2490` (`max_context_tokens` property, also defaulting to 128000) **MEASURED** |
| 2 OWNER | Two owners for one number: the `TokenCounter` method and the `WorkerThread` property (the latter's docstring says it uses "the same lookup as `TokenCounter.get_model_context_window()`" — `worker_thread.py:2476` — but it is a copy, not a call) **MEASURED**. A third declaration exists as a workspace capability key `max_context_length` (`thoughtmachine/workspace_capabilities.py:77-78`, `:107`; `0` = no restriction) — serialised only **INFERRED** (no read site found; see row 3) |
| 3 READ | `worker_thread.max_context_tokens` is read at ~15 envelope-construction sites, always paired with `current_context_tokens` and always as a **reported field**: `worker_thread.py:4453`, `:4894`, `:5034`, `:5047`, `:5090`, `:5103`, … **MEASURED**. `TokenCounter.get_model_context_window` has **no caller**: the only occurrence outside its definition is a docstring mention (`worker_thread.py:2476`) — searched `agent/`, `tools/`, `session/`, `thoughtmachine/` **MEASURED** (negative search) → the method itself is **INFERRED** dead. `max_context_length` is read only by the capability reporter (`tools/workspace/check_system.py:976`, `:1419`) **MEASURED** |
| 4 ENFORCED | **NONE anywhere.** Searched `worker_thread.py` for `current_context_tokens >` / `context_ratio` / `percent` — no comparison site exists; every hit is a report pair **MEASURED** (negative). Nothing sizes or trims the prompt against the window before the call; the only reaction is *after* the provider refuses (B4) **MEASURED** |
| 5 LOGGED | The pair is emitted in worker status/partial envelopes (rows above) **MEASURED**. The agent-side INFO line that *should* carry it explicitly says it does not: `log('INFO','core.context', f"... est_tokens=N/A")` — `session/context_builder.py:373` **MEASURED**. The real estimate is logged one block later as a separate `LLM context built` DEBUG record (`session/context_builder.py:377-386`) **MEASURED** |
| malformed | Constants/lookup tables, not config: no parse path. An unknown model silently gets `default 128000` in both copies (`token_counter.py:162-171`, `worker_thread.py:2493`) — the failure mode is "assume 128k", never "refuse" **MEASURED** |
| reported-vs-cutting | Reported (`max_context_tokens`) and cutting (provider error → B4) are unrelated mechanisms **MEASURED** → F |

### B4 — the only token-driven trim: `emergency_mode` (skip oldest 20 %)

| Q | Answer |
|---|---|
| 1 DECLARED | A boolean, no number: `self.emergency_mode = False` — `session/context_builder.py:271`; delegated property on the history provider — `session/history_provider.py:55-62` **MEASURED**. The trim fraction `0.2` is a **literal at the use site** — `session/context_builder.py:332` **MEASURED** |
| 2 OWNER | `SummaryBuilder` instance state; the *trigger* is owned by `Agent._emergency_retries` (`agent/core/agent.py:1568`, `:2016-2032`) **MEASURED** |
| 3 READ | `session/context_builder.py:330-334` (`if self.emergency_mode and non_system: skip_count = max(1, int(len(non_system) * 0.2)); non_system = non_system[skip_count:]`) **MEASURED** |
| 4 ENFORCED | Yes, but **reactively**: the flag is only set after the provider raises `LLMError(error_type='token_limit_exceeded')` (`agent/core/agent.py:2031`, error classified at `agent/core/llm_client.py:398-408`); at most `2` retries (`agent.py:2017`), then a hard `error` event with `stop_reason: 'context_full'` and `return` (`agent.py:2019-2028`). So the *cutting* ceiling is the provider's, and the local recovery is bounded by a literal retry count **MEASURED** |
| 5 LOGGED | `log('WARNING','core.agent','Token limit exceeded – emergency retry', {'attempt': ...})` (`agent.py:2033-2034`); `log('INFO','core.context', f'[EMERGENCY] Skipping oldest {skip_count}/{len(non_system)} ...')` (`session/context_builder.py:333`); failure path `log('ERROR', ...)` (`agent.py:2020-2021`) **MEASURED**. The *provider's* stated limit is not logged separately — `{'error': str(e)}` only, in the failure branch (`agent.py:2021`) **MEASURED** |
| malformed / zero / negative | `0.2` and `2` are literals, not config: nothing to mis-set. `max(1, ...)` guarantees at least one message is dropped per emergency pass (`context_builder.py:332`) **MEASURED** |
| reported-vs-cutting | Not reported as a number anywhere; the user-visible outcome is the `context_full` error text (`agent.py:2025-2028`) **MEASURED** |

### B5 — response-length ceiling: declared twice, used nowhere

| Q | Answer |
|---|---|
| 1 DECLARED | `DEFAULT_RESPONSE_TOKENS = 4096` — `agent/config/defaults.py` (section `agent/core/agent.py`), imported and re-bound as a class attribute `Agent.DEFAULT_RESPONSE_TOKENS = DEFAULT_RESPONSE_TOKENS` — `agent/core/agent.py:52`, `:540`; plus `SAFETY_MARGIN = 1000` — `agent/core/agent.py:539` **MEASURED** |
| 2 OWNER | `agent.config.defaults` (module constant) and the `Agent` class attribute that shadows it (same object) **MEASURED** |
| 3 READ | **No read site found.** Searched `agent/**/*.py` for `DEFAULT_RESPONSE_TOKENS|SAFETY_MARGIN|max_tokens`: the only hits are the import (`agent.py:52`) and the two class-attribute assignments (`agent.py:539-540`) **MEASURED** (negative search) → both are **INFERRED** dead |
| 4 ENFORCED | **NONE, and no substitute.** The completion call is built as `chat_kwargs = {'temperature': ...}` (+ optional `top_p`) — `agent/core/agent.py:1844-1846` — and is spread with no output bound; `agent/core/llm_client.py` contains **no** `max_tokens`/`max_completion_tokens`/`4096`/`2048` occurrence (grep over that file: 0 matches) **MEASURED** → response length is entirely provider-default |
| 5 LOGGED | **NONE.** Output tokens are only *observed* after the fact (`output_tokens`, `agent.py:1869`, `:2019-2021` region; telemetry `agent/core/...` token_usage at `worker_thread.py:5504-5509`) **INFERRED** for the telemetry pairing, **MEASURED** for the class-attribute/`chat_kwargs` facts |
| malformed | Constants; no parse path. **MEASURED** |
| reported-vs-cutting | Nothing reported, nothing cut **MEASURED** → F (the one ceiling a "429/context" incident would first blame is absent) |

### B6 — the turn ceiling (the only hard stop in the agent loop)

| Q | Answer |
|---|---|
| 1 DECLARED | `max_turns` is a config field (`agent/config/models.py`, `turn_monitor_enabled` default `True` at `:191`); the loop bound is `for turn in range(self.config.max_turns)` — `agent/core/agent.py:1695` **MEASURED** |
| 2 OWNER | `AgentConfig.max_turns` (hot-swap aware: read fresh at loop entry, `agent.py:1691-1694`) **MEASURED** |
| 3 READ | `agent.py:1695`, and `state.update_turn_state` `state.py:317-325` (`critical_turn = max(max_turns - 5, 1)`, `warning_turn = max(max_turns - 8, 0)`) **MEASURED** |
| 4 ENFORCED | Yes — `range()` ends the loop; WARNING at `max_turns-8` is prose only, CRITICAL at `max_turns-5` sets `restrictions_active` / `restriction_reason='turn'` (`state.py:327-364`) **MEASURED**. This is the *only* ceiling in the agent loop that actually terminates work — the clock (A3/A8) and the token monitors (B1) do not **MEASURED** |
| 5 LOGGED | `log('DEBUG','pipeline.warning', ...)` at `state.py:340`, `:356`; warning prose carries `{current_turn}/{max_turns}` (`state.py:342`, `:361`); terminal log `log_max_turns_reached` (`agent/logging/__init__.py:578-580`, message includes the configured `max_turns`) **MEASURED** |
| malformed / zero / negative | `max_turns = 0` → `range(0)` → zero turns, no explicit error (`agent.py:1695`) **INFERRED** (plain `range` semantics). Small values are handled defensively in the thresholds (`state.py:323-325`) **MEASURED** |
| reported-vs-cutting | Same number in both places (`state.py:342`/`:361` prose vs `agent.py:1695` bound) **MEASURED** |


---

## C. Tool-output / content ceilings

### C1 — the shared mechanism: `BaseTool.token_limit` + `_truncate_output`

| Q | Answer |
|---|---|
| 1 DECLARED | `token_limit` field on `BaseTool` (`tools/base.py:53`); default value comes from config, not from the tool (`agent/core/tool_executor.py:548-549`, source `agent/config/models.py:212` = 10000) **MEASURED**; `skip_output_truncation` ClassVar opt-out (`tools/base.py:149-151`) **MEASURED** |
| 2 OWNER | `AgentConfig.tool_output_token_limit`; the `BaseTool` field is a **delivery channel**, not an owner (its own default is `None`) **MEASURED** |
| 3 READ | `tools/base.py:457-461` (`limit = self.token_limit`), applied by every tool that wraps its return in `self._truncate_output(...)` — e.g. `tools/docker_code_runner.py:301`, `:326`, `:338`, `:349`, `:369`, `:487`, `:501`, `:513`, `:521`, `:530` **MEASURED** |
| 4 ENFORCED | `limit is None` or `<= 0` → **no truncation** (`tools/base.py:457-461`); else `target_chars = limit*4`, prefer last `\n` past 80 % of target, else `-100` chars in a loop until it fits (`tools/base.py:457-488`) **MEASURED** |
| 5 LOGGED | Announced in-band on the cut: `"\n... (output truncated to {limit} tokens, original was {estimated_tokens} tokens)"` (`tools/base.py:485-488`); estimated with `len(text)//4` (`tools/base.py:452-455`) **MEASURED** |
| malformed / zero / negative | `0`/negative → **silently unlimited** (`tools/base.py:457-461`) — the failure mode of a mis-set limit is *more* output, not less, and nothing says so **MEASURED** |
| reported-vs-cutting | Same value cuts and is named in the notice; the *unit* is chars/4 (`tools/base.py:452-455`) while B1's monitors use the tokenizer/estimator → the announced ``tokens'' are a different quantity **MEASURED** |

### C2 — per-tool caps (declared → enforced → announced)

Each line: **DECLARED** anchor · **ENFORCED?** · **ANNOUNCED?**

- **FileSearch** — `MAX_FILE_SIZE=10_000_000`, `MAX_LINES_PER_FILE=100_000`, `MAX_CONTEXT_LINES=20`, `MAX_OUTPUT_CHARS=40_000`, `MAX_RESULTS=200`, `MAX_FILES_TO_SEARCH=1000` (`tools/file_search_tool.py:19-24`) **MEASURED**; enforced with three escalating warnings: context dropped first (`:404-415`), then a `CHAR_BUDGET_WARNING` (`:433-455`), then a hard `while len(output) > MAX_OUTPUT_CHARS: pop` loop (`:457-463`) **MEASURED**; pinned by tests — `tests/test_file_search_output_grouping.py:113-125` asserts `len(out) <= FileSearchTool.MAX_OUTPUT_CHARS` and that the notice names the budget **MEASURED** → this is the one fully honest cap.
- **FilePreview** — `MAX_HEAD_LINES=500`, `MAX_TAIL_LINES=500`, `MAX_TOTAL_LINES=1000`, `MAX_OUTPUT_CHARS=30_000` (`tools/file_preview_tool.py:13-16`) **MEASURED**. Head/tail/total are enforced by a proportional reduction (`:99-112`) and silently (no notice) **MEASURED**. `MAX_OUTPUT_CHARS` has **no other occurrence in the file** and **no test references it** (searched `tools/`, `tests/`) → **INFERRED** declared-but-not-enforced. The 10 MB input cap is a *field* default, not a constant: `max_file_size: int = Field(default=10_000_000, ...)` (`:23`) with an explicit error string (`:42-44`) **MEASURED** — i.e. the class constant list and the actual gate disagree in form.
- **JobRegistry previews** — `PREVIEW_CAP = 8000` (`agent/config/defaults.py:109`), `PARTIAL_PREVIEW_CAP = 2000` (`:111`), imported at `tools/workspace/job_registry.py:32-36` **MEASURED**; enforced by raw slicing — `str(...)[:PARTIAL_PREVIEW_CAP]` (`job_registry.py:108`, `:115`, `:120`) and `[:PREVIEW_CAP]` (`:231`, `:282`) **MEASURED**; **ANNOUNCED? NO** — a sliced preview carries no marker, so a truncated partial result is indistinguishable from a short one (**MEASURED** for the absence of any notice call in those statements).
- **Container exec output** — `EXEC_OUTPUT_LIMIT_BYTES = 100*1024` (`agent/config/defaults.py:269`) **MEASURED**; enforced byte-wise in `infra/container_manager.py:611-621` and at three call sites — build log (`:4603-4604`), stdout/stderr (`:4657-4660`) **MEASURED**; announced with `_TRUNCATION_NOTICE` (`:621`, `:4658`, `:4660`) — the notice's exact text is **UNMEASURED** (not read).
- **Git show_file** — `_SHOW_FILE_MAX_READ_BYTES = 4*1024*1024` (`tools/git_info_tool.py:39-43`) hard-bounded read (`:2405`, `:2414-2416`) and **announced in the header** (`:2440-2444`, "first N bytes only: the file is larger") **MEASURED**. `_SHOW_FILE_MAX_SPAN_FALLBACK = 2000` lines (`:36-37`, used at `:2579`) is a fallback when no token budget exists → **INFERRED** silent (no announcement anchor found).
- **Git log** — `max_count` clamped `min(1000, raw)` with an explicit parse error but **no clamp notice** (`tools/git_info_tool.py:1803-1806`) **MEASURED** → silent ceiling.
- **ci_read** — `_MAX_PER_PAGE = 100` (`tools/ci_read_tool.py:74-75`), clamp `max(1, min(_MAX_PER_PAGE, number))` (`:293-299`) **MEASURED**, silent (the description string advertises the range at `:199`, but a clamped request is not reported) **MEASURED**.
- **KnowledgeBase** — `max_tokens` request field (`tools/knowledge_base.py:363-364`) and a docstring promise of auto-truncation above 4000 tokens (`:1106-1108`) **MEASURED**; enforced with an explicit, self-describing notice (`:1150-1161`) **MEASURED**. The 4000-token auto branch is in the same block (`:1165-1176` region) — **UNMEASURED** in this pass (not read line-by-line).
- **FileSummary / DirectoryTree** — count caps as **fields**, not constants: `max_methods_per_class=10`, `max_functions=50` (`tools/file_summary_tool.py:20-21`), enforced with "... and N more" lines (`:173-174`, `:188-189`) **MEASURED** → announced. `max_results=100`, `max_depth=2` (`tools/directory_tree_tool.py:37`, `:43`) with a `skipped` counter (`:127-129`) **MEASURED**.

**Section-level read:** of nine caps surveyed, **five announce** (BaseTool truncation, FileSearch, container exec, git show_file, FileSummary) and **four are silent** (FilePreview head/tail reduction, JobRegistry previews, git log clamp, ci_read clamp); one is **declared and not enforced at all** (`FilePreview.MAX_OUTPUT_CHARS`) — all **MEASURED** except where labelled otherwise above.

---

## D. Process / container ceilings (infra layer)

| Cap | Declared | Owner | Read / enforced | Logged |
|---|---|---|---|---|
| exec call timeout `30 s` | **Literal default in three signatures**: `infra/containers/executor.py:1089` (`execute(timeout=30)`), `:1161` (`_exec_with_timeout(timeout=30)`), `infra/container_manager.py:2529` (`exec(timeout=30)`), `infra/resource_container_manager.py:1520` **MEASURED** | the three call sites themselves — **no shared constant** (searched `infra/` for a named exec-timeout constant: none) **MEASURED** (negative) | passed straight to the docker SDK `exec_run`/`wait` path — the cut is docker's, at 30 s unless the caller overrides **INFERRED** (SDK semantics; no local watchdog seen) | the timeout surfaces as a raised/returned error string, not a dedicated log line **INFERRED** |
| graceful stop `STOP_TIMEOUT = 10` | `agent/config/defaults.py:265` **MEASURED** | `agent.config.defaults`, read by `infra/container_registry.py:131-132`, `:48` (re-export) **MEASURED** | `container.stop(timeout=STOP_TIMEOUT)` at `infra/container_registry.py:813` (destroy) and `:874` (permission-change teardown) **MEASURED** | destroy path emits `emit_destroy_audit(...)` (`:810-812`, `:871-873`) which names the reason but **not** the timeout **MEASURED** |
| other stop timeouts | `infra/container_manager.py:3733` `container.stop(timeout=grace)` (caller-supplied); `infra/resource_container_manager.py:1815`, `:1872` `stop(timeout=5)` **MEASURED** (literals) | call sites | enforced at call | **UNMEASURED** |
| idle teardown | see **A2** (cross-ref, not restated) | | | |
| PID ceiling | `DEFAULT_PIDS_LIMIT = 512` (`agent/config/defaults.py:263`) **MEASURED** | `agent.config.defaults` | passed to **every** create: `infra/container_create.py:289` (`pids_limit: int \| None = DEFAULT_PIDS_LIMIT`), `:452-455` (`pids_limit=spec.pids_limit`, comment: `None` "would leave HostConfig.PidsLimit unset"); `infra/container_registry.py:150` **MEASURED** | not logged per container; the *absence* is what the comment protects against **MEASURED** (comment read) |
| memory / cpu | `DEFAULT_MEM_LIMIT = "512m"`, `DEFAULT_CPU_QUOTA = 100000` (`agent/config/defaults.py:260-261`) **MEASURED** | `agent.config.defaults` | `ContainerSpec`/`CreateSpec` defaults (`infra/container_create.py:287-288`, `infra/container_registry.py:148-149`) → passed to `mem_limit` / `cpu_quota` (`container_create.py:450-451`) **MEASURED** | not logged per container **INFERRED** |
| tmpfs size | `/tmp` ← `EXEC_TMPFS_OPTIONS`, `/home/agent` ← `"rw,exec,size=256m"` (`agent/config/defaults.py:236-237`, `:253-257`) **MEASURED** | `agent.config.defaults` | applied as container `tmpfs=` (`infra/container_create.py:456`) **MEASURED**; the *value* of `EXEC_TMPFS_OPTIONS` is **UNMEASURED** (not read) | not logged **INFERRED** |
| host shell execution `120 s` | `HOST_BASH_TIMEOUT = 120` **twice**: `agent/config/defaults.py:118` and `tools/host_bash_tool.py:61`; `HOST_BASH_APPROVAL_TIMEOUT = 120.0` **twice**: `defaults.py:120` and `host_bash_tool.py:62` **MEASURED** | the **tool's** copy is the live owner; the `defaults.py` copies have **no reader in `agent/`** (searched: only the declaration) → **INFERRED** dead, while `host_bash_tool.py:58-60` states the duplication is deliberate ("*The shared `agent.config.defaults` module is not present in every deployment, so the values live here*") **MEASURED** | `timeout = int(os.environ.get("HOST_BASH_TIMEOUT", str(HOST_BASH_TIMEOUT)))` (`host_bash_tool.py:311`) — **env-overridable, no validation** (a non-numeric env value raises `ValueError` at `int(...)`) **MEASURED**; approval wait uses the same pattern with `float(...)` (`:219`) and a `queue.Empty` → "denied" path (`:222`, `:308-309`) **MEASURED** | approval denial/audit: `self._audit_log("deny", "host_bash: security approval timed out", cmd)` and `denied_json(..., outcome="approval_timeout")` (`:308-309`) **MEASURED** — this is one of the few timeout paths that *reports* itself with a distinct outcome |
| container build | `idle_timeout=0` for ephemeral builders (`infra/containers/executor.py:1483`) **MEASURED**; build log byte-capped (`infra/container_manager.py:4603-4604`) **MEASURED** | call site | enforced (A2 pattern + C2) | truncation announced via `_TRUNCATION_NOTICE` **MEASURED** |


---

## D-AMENDMENT — A7 row 3/4/5 (watchdog file now read)

**A7 amendment (supersedes the three `UNMEASURED` cells in A7).** The `WORKER_STALE_*` ladder **is** read as per-instance defaults: `tools/workspace/worker_stale_watchdog.py:112-118` (import of all five constants from `agent.config.defaults`), `:279-287` (`interval`, `announce_after`, `probe_timeout`, `cancel_timeout`, `kill_timeout` default arguments) — **MEASURED**. The ladder is therefore *armed*, not dead, and the ANNOUNCE-ONLY claim in `agent/config/defaults.py:91-95` is carried by the module's own contract: `kill` is the "only terminating verb in this module, reachable from exactly one function that the scan path cannot call" (`worker_stale_watchdog.py:37-40`, pinned by `tests/test_worker_stale_watchdog.py`) and the observer is given a **private** `EventBus()` whose announcements "nothing consumes" (`:50-53`) — **MEASURED** (prose read at `:2-53`). So: budgets READ at `:279-287`, cutting verbs present but unreachable from the scan path, announcements routed to a bus with no subscriber. Logging of the ladder **remains UNMEASURED** (only the docstring end was read; no log call was inspected).

---

## E. Declaration hygiene — duplicates, dead ceilings, drift, and what the tests pin

### E1 — one value, many independent literals (no derivation edges)

| Value | Every declaration | Tied to each other by code? |
|---|---|---|
| **600 s** | `thoughtmachine/timebudget.py:67` (`DECLARED_CEILING`); `agent/config/defaults.py:33` (`SPAWN_QUEUE_TIMEOUT`); `:43` (`HEARTBEAT_STALE_AFTER_S`); `:61` (`WORKER_TIMEOUT_SECONDS`); `:131` (`HARD_TIMEOUT`) **MEASURED** | **No.** There is **no import edge** between `timebudget` and `defaults` in either direction (`timebudget.py:59-63` imports `numbers`, `dataclasses`, `typing`, `timeout_constants` only) **MEASURED**. The only tie is a *test*: `tests/test_time_budget_contract.py:221-227` asserts `DECLARED_CEILING == 600` **and** `DECLARED_CEILING == defaults.HARD_TIMEOUT` — i.e. 2 of the 5 are pinned, 3 are not **MEASURED** |
| **300 s** | `thoughtmachine/timeout_constants.py:28` (`SOFT_BUDGET_FALLBACK_SECONDS`); `:30` (`SHIPPED_SOFT_BUDGET_SECONDS`); `agent/config/defaults.py:130` (`SOFT_TIMEOUT`) **MEASURED** | Partly. `timeout_seconds == SOFT_BUDGET_FALLBACK_SECONDS` is pinned (`tests/test_task4_config_propagation.py:57-61`); `tests/test_worker_lifecycle_trust.py:432` pins `SOFT_TIMEOUT == 300` **as a literal**, so it would survive a change to `SOFT_BUDGET_FALLBACK_SECONDS` — the two 300s are not tied **MEASURED** |
| **two different defaults for the config key `timeout_seconds`** | `300` at `agent/core/state.py:102` and `agent/config/models.py:223`; **`600`** at `tools/workspace/check_system.py:647` (`cfg.get("timeout_seconds", IDLE_TIMEOUT_SECONDS)`) **MEASURED** | **No.** Same key, two values, chosen by which module reads it **MEASURED** — see F-1 |
| **30 s exec timeout** | `infra/containers/executor.py:1089`, `:1161`; `infra/container_manager.py:2529`; `infra/resource_container_manager.py:1520` **MEASURED** | No shared constant exists (negative search over `infra/`) **MEASURED** |
| **10 MB file size** | `tools/file_preview_tool.py:23` (field default `10_000_000`) and `tools/file_search_tool.py:19` (`MAX_FILE_SIZE = 10_000_000`) **MEASURED** | Not tied; one is a pydantic field default, the other a ClassVar **MEASURED** |

### E2 — ceilings that are declared and have **no runtime reader** (dead)

| Constant | Declared at | Only reader found | Label |
|---|---|---|---|
| `SHIPPED_SOFT_BUDGET_SECONDS` | `thoughtmachine/timeout_constants.py:30` | re-export only (`agent/config/defaults.py:28`) | **INFERRED** dead (searched `agent/`, `tools/`, `session/`, `infra/`, `tests/` for the name: no functional read) |
| `SPAWN_QUEUE_TIMEOUT` | `agent/config/defaults.py:33` | `tests/test_worker_lifecycle_trust.py:414` (value pin) | **MEASURED** that the only reader is a test → **INFERRED** dead at runtime |
| `HARD_TIMEOUT` (as a runtime bound) | `agent/config/defaults.py:131` | import at `infra/workspace_lifecycle_manager.py:75`, **no other occurrence under `infra/`** (searched) | **MEASURED** (negative search) → the "hard" 600 s ceiling has no kill path there **INFERRED** |
| `EXEC_KILL_GRACE` | `agent/config/defaults.py:132` | imported `infra/workspace_lifecycle_manager.py:75` (no use); read in `tools/workspace/worker_execution.py` (identity-pinned at `tests/test_worker_lifecycle_trust.py:438-439`) | **MEASURED** imports; whether it bounds a real kill **UNMEASURED** |
| `HOST_BASH_TIMEOUT`, `HOST_BASH_APPROVAL_TIMEOUT` (the `defaults.py` copies) | `agent/config/defaults.py:118`, `:120` | **none in `agent/`** (searched) — the live copies are `tools/host_bash_tool.py:61-62`, read at `:219`, `:311` | **MEASURED** (negative search) → **INFERRED** dead in `defaults.py` |
| `SAFETY_MARGIN`, `DEFAULT_RESPONSE_TOKENS` | `agent/core/agent.py:539-540` | none (see B5) | **MEASURED** (negative search) → **INFERRED** dead |
| `FilePreview.MAX_OUTPUT_CHARS` | `tools/file_preview_tool.py:16` | none in `tools/`, no test | **INFERRED** dead / not enforced (C2) |
| `TokenCounter.get_model_context_window` | `agent/core/token_counter.py:150` | none (docstring mention only) | **MEASURED** (negative search) → **INFERRED** dead |
| `max_context_length` (capability) | `thoughtmachine/workspace_capabilities.py:77-78`, `:107` | `tools/workspace/check_system.py:976`, `:1419` (reported) | **MEASURED** → **INFERRED** never enforced |

### E3 — prose that contradicts code

- `thoughtmachine/timebudget.py:41` says `defaults.HARD_TIMEOUT` is *"re-exported from here"*; `defaults.py` never imports `timebudget` and `timebudget` never imports `defaults` → wrong direction of a real duplication **MEASURED**.
- `CEILING_OWNER` is a **string** naming `agent.config.defaults.HARD_TIMEOUT` (`thoughtmachine/timebudget.py:72-75`); nothing compares it to the module it names — only the test in E1 does **MEASURED**.
- `tests/test_time_budget_contract.py:46-49` still asserts *"The module does not exist yet — that is the behaviour under test, not an import typo"*, while `thoughtmachine/timebudget.py` exists and exports `TimeBudget`, `TimeBudgetExceeded`, `DECLARED_CEILING` (A1) → **stale prose** **MEASURED**.
- `tools/base.py:452-455` says the tiktoken implementation was *"disabled due to network issues in Docker"* and uses `len(text)//4`; yet `session/context_builder.py:109`, `:379` and `agent/core/token_counter.py:97-102` still call `tiktoken.get_encoding('cl100k_base')` with `len//4` only as a *fallback* **MEASURED** → three estimators coexist: `token_counter` (tiktoken→`len//4`), `context_builder._estimate_tokens` (delegates to `token_counter`, `session/context_builder.py:93-114`), `base._estimate_tokens` (plain `len//4`, `tools/base.py:452-455`). A tool-result "token" and a monitor "token" are therefore different units **MEASURED**.
- `tools/workspace/worker_thread.py:2476` claims its window table uses *"the same lookup as `TokenCounter.get_model_context_window()`"* while it is a separate copy that **can** drift (`:2481-2490` vs `agent/core/token_counter.py:158-161`; the copy drops the `step-3.5` key and adds a `claude` catch-all) **MEASURED**.

### E4 — good-hygiene counter-example (worth keeping)

`tools/workspace/job_registry.py:31-36` imports `PREVIEW_CAP`, `PARTIAL_PREVIEW_CAP`, `JOB_REGISTRY_MAX_JOBS`, `TERMINAL_STATUSES` from `agent.config.defaults` instead of re-declaring them, with the comment that the names stay on the module "so existing importers/tests keep seeing the same names" **MEASURED**; object identity is pinned in `tests/test_worker_lifecycle_trust.py:434-439` **MEASURED**.

### E5 — what a refactor would break (tests that pin ceilings)

- `tests/test_worker_lifecycle_trust.py:409-440` — 18 literal value pins + 4 object-identity pins (`SPAWN_QUEUE_TIMEOUT=600`, `QUERY_WAIT_GRACE_SECONDS=60`, `HEARTBEAT_INTERVAL_S=30`, `HEARTBEAT_STALE_AFTER_S=600`, `PREVIEW_CAP=8000`, `PARTIAL_PREVIEW_CAP=2000`, `SOFT_TIMEOUT=300`, `HARD_TIMEOUT=600`, `EXEC_KILL_GRACE=10`) **MEASURED**.
- `tests/test_time_budget_contract.py:145-152` (refusal names request **and** ceiling **and** one of `("hard_timeout","timeout_constants","declared_ceiling")`), `:156-171` (clamp reported in the envelope), `:174-189` (the 420→300 acceptance test), `:199-211` (wait and envelope share one clock), `:221-227` (one named constant) **MEASURED**. Note `:174-189` is written to *fail on the current defect* ("silent clamp: requested 420, got 300"), not to pin a number.
- `tests/test_config_layering.py:607-613` — `IDLE_TIMEOUT_SECONDS == 600` **MEASURED**.
- `tests/test_task4_config_propagation.py:57-61` — `AgentConfig.timeout_seconds == SOFT_BUDGET_FALLBACK_SECONDS` **MEASURED**.
- `tests/test_file_search_output_grouping.py:113-125` — the char budget is *really* respected and *really* announced **MEASURED**.
- **Not covered by any test** (searched `tests/` for each name, negative): `FilePreview.MAX_OUTPUT_CHARS`, the two `HOST_BASH_*` copies agreeing, `SAFETY_MARGIN`/`DEFAULT_RESPONSE_TOKENS`, `get_model_context_window`, and the two model→window tables agreeing **MEASURED** (negative searches) / **INFERRED** (that the omission is accidental).

---

## F. Reported-vs-cutting gaps and the undecidable residue

| # | Gap | Reported number / hop | Cutting number / hop | Label |
|---|---|---|---|---|
| F-1 | **Two defaults for one config key.** The soft budget the model is told about is 300 (`state.py:102` / `models.py:223`); the same key read by `check_system.py:647` defaults to 600 | `state.py:255-257` prose ("running for Xs (Ys remaining)") built from `config.timeout_seconds` | the agent loop does **not** cut at all; callers cut at their own bound (300 / 600 / 60 per A1) | **MEASURED** both ends |
| F-2 | **Grace excluded from the reported wait.** `_worker_query_wait_timeout` returns `budget.effective + QUERY_WAIT_GRACE_SECONDS` (`worker_timeout.py:105`) but the envelope's `effective_timeout_seconds` comes from the earlier `TimeBudget` (`worker_thread.py:6721-6722`) | `effective_timeout_seconds` | actual wait = effective + 60 | **MEASURED** |
| F-3 | **No response-length ceiling at all**, though two constants declare intent (`agent.py:539-540`), and the call passes none (`agent.py:1844-1846`; no `max_tokens` in `llm_client.py`) | none | provider default | **MEASURED** |
| F-4 | **Context window is reported, never compared.** `max_context_tokens` pairs in ~15 envelopes; no comparison site; reaction is post-hoc emergency trim after a provider error | `worker_thread.py:4453` etc. | provider error → `emergency_mode` (B4) | **MEASURED** (negative search for the comparison) |
| F-5 | **Unit mismatch.** Tool notices announce "tokens" computed as `len//4` (`tools/base.py:452-455`), while the monitors use `token_counter` (tiktoken → `len//4` fallback, `token_counter.py:44-51`) | tool-result notice | monitor thresholds (B1) | **MEASURED** |
| F-6 | **Silent caps.** `git log` clamp 1000 (`git_info_tool.py:1803-1806`), `ci_read` clamp 100 (`ci_read_tool.py:293-299`), job preview slices (`job_registry.py:108-282`), FilePreview head/tail reduction (`file_preview_tool.py:99-112`) | no marker emitted | the cap is applied | **MEASURED** |
| F-7 | **Declared cap not enforced.** `FilePreview.MAX_OUTPUT_CHARS = 30_000` has no reader and no test → output can exceed the declared budget while `FileSearch`'s equivalent is enforced and tested | declared 30 000 chars | unchecked | **INFERRED** (searched: no occurrence/test) |
| F-8 | **"Hard" ceiling with no kill path.** `HARD_TIMEOUT` + `EXEC_KILL_GRACE` imported by `infra/workspace_lifecycle_manager.py:75` but unread there; the ladder that *does* kill is announce-only-by-construction (D-amendment) | envelope ceiling 600 | caller deadline only | **MEASURED** (imports/negative search) + **INFERRED** (consequence) |
| F-9 | **Announced truncation can be wrong in the other direction.** With `token_limit` `None` or `<= 0` the shared truncator is *off* — a mis-set limit yields unlimited output and no notice (`tools/base.py:457-461`) | nothing | nothing | **MEASURED** |

### Undecidable from this repo alone (**UNMEASURED**)

1. Whether the provider enforces its own output-token limit (F-3) — external to the repo.
2. The exact text/byte-count of `infra/container_manager.py`'s `_TRUNCATION_NOTICE` (referenced at `:621`, `:4658`, `:4660`; definition not read).
3. The value of `EXEC_TMPFS_OPTIONS` (used at `agent/config/defaults.py:236-237`).
4. Whether `EXEC_KILL_GRACE` bounds a real kill anywhere in `tools/workspace/worker_execution.py` (only its identity import was observed).
5. Client-side (JS) ceilings in `web_ui/`: the Python search for `effective_timeout_seconds` / `requested_timeout_seconds` / `max_context_length` over `web_ui/**/*.py` returned **no matches**, so nothing in the Python UI consumes the envelope's timeout fields **MEASURED**; the browser-side caps are **UNMEASURED**.
6. Live values after hot-swap: every number above is a *declaration* or a *default*; the effective runtime value for a given session (config file overlay, `agent_soft_budget_seconds`, `tool_output_limit`) was not exercised.

