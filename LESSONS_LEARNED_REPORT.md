# Lessons Learned Report

## Worker Event Pipeline & Token Warning System Refactoring

**Period:** Commit `e194216` → `776677d` | **Baseline:** July 9, 2026 | **HEAD:** July 16, 2026  
**Scope:** ~200 commits, ~1 week of development, 6 key files modified across backend, frontend, and core agent

---

## 1. Timeline

### July 9 — The Baseline

The period starts at commit `e194216` ("chore: checkpoint before token event fix"). At this point the system had:

- A working **main agent** that emitted events through `Bridge._map_and_emit()` — this was the stable, verified path
- A **worker agent** that used manual `_publish_event()` calls inside `_run_tool_loop()` — each event type had its own `if event_type == "..."` block with bespoke field handling
- A **per-worker EventBus** infrastructure had been built (commits `c5c4eaf` → `588867f` → `a68383e`) but was not yet fully wired
- A **full pipeline audit** (`588867f`, "full pipeline audit + fixes for worker/main agent parity") had identified divergences between main agent and worker event handling, but the fixes were incomplete

**Key observations about the baseline:**
- Worker event publishing was ad-hoc: each event type was manually handled with copy-pasted code in `_run_tool_loop()`
- There was **no EventProcessor** integration — the worker didn't use the presenter layer at all
- The `[SYSTEM NOTIFICATION]` for context summarization was **never yielded** by `process_query()` — it was injected directly into `user_history` as a message dict, so the event stream never saw it
- The worker used a **heuristic** to detect summarization: if the token count dropped by ≥40%, it manufactured a `system_notification` event

### July 9-10 — The First Fix Wave

| Commit | Description | What Actually Happened |
|--------|-------------|----------------------|
| `d0af745` | "fix: emit tokens_updated on session load" | Added token emission at session load time — status bar no longer frozen at 0/0 |
| `ea2ab6a` | "fix: forward token_monitor thresholds to workers" | Fixed bridge subscription race, restored production defaults, removed debug scaffolding |
| `a0f59e9` | "fix: worker event pipeline — auto-inject worker_name, forward events to global bus" | Added `worker_name` injection, fixed WebUI warning field names |

These were genuine fixes but they were **patching around the edges** — the fundamental divergence between main agent and worker event paths remained.

### July 10-14 — Scope Creep: The `worker_state_sync` Era

Between July 10-14, the team implemented a series of increasingly complex workarounds:

1. **`worker_state_sync`** was introduced — a synthetic event type that polled the agent's internal state and forwarded it to the frontend. This was necessary because the real event types (`token_update`, `context_updated`) weren't flowing correctly through the worker path.

2. **Event-level dedup** was added to the bridge's per-worker bus handler — comparing formatted display strings to skip duplicate `context_updated` events.

3. **Custom timestamp-based dedup** was added in the frontend's `WorkerOutputPanel` — comparing `(eventType, timestamp)` tuples.

4. **Time-based throttling** was added in multiple layers.

**The fundamental problem:** Each layer was compensating for the layer below it, rather than fixing the root cause. The bridge added dedup because the frontend was getting duplicates. The frontend added timestamp dedup because the bridge didn't dedup correctly. Neither fixed the actual duplication source.

### July 14 — The Unified Presenter Pipeline

Commit `06cd7a0` ("feat: unified presenter pipeline for workers") was a significant refactoring that:

1. Added `WorkerBusAdapter` — a ~200-line class that replaced the manual `_publish_event()` calls in `_run_tool_loop()`
2. Added `EventProcessor` integration — wire the worker's agent events through the same `EventProcessor` that the main agent uses
3. Added `StateBridge` — a shared state object that `EventProcessor` updates, providing a single source of truth for token counts

**This was the right architectural decision**, but the implementation had problems:

- The `WorkerBusAdapter.emit_tokens_updated()` and `emit_context_updated()` methods were initially **never called** — they were defined but not wired into `_run_tool_loop()`
- The `EventProcessor`'s `_process_token_update_event` method had dead code from copy-paste — it checked for `token_warning`, `turn_warning`, etc. inside a method named `_process_token_update_event`
- The heuristic for detecting context summarization (token drop ≥40%) was **retained** even though we now had real event types (`context_summarized`, `context_cleared`)
- The `worker_state_sync` flood was still in place — called after EVERY agent event

### July 14-15 — The Breaking Point

| Commit | Description | Impact |
|--------|-------------|--------|
| `7e1ff43` | "fix: populate max_context_tokens from critical_threshold" | Built more scaffolding on top of the broken system |
| `bdba5ca` | **CORRUPTED**: "Fix SyntaxError in agent/core/agent.py" | **Corrupted file** — duplicated blocks introduced during a bad merge |
| `26a3b53` | **EMERGENCY**: "broken code but need to go back" | Raw admission that the refactoring had broken the core agent |
| `a5d56c6` | "fix: resolve stop bug — return→continue + remove duplicate warning buffer" | Fixed a `return` statement that should have been `continue`, breaking the tool loop |
| `11ae368` | "fix: core stability after SummarizeTool" | Agent survived post-summary continuation |

**This was the low point.** The system had:
- A corrupted `agent.py` with duplicated blocks
- A `return→continue` bug that prevented the agent from continuing after summarization
- A `self._bus → self._worker_bus_adapter` reference that wasn't updated, crashing workers
- The heuristic summarization detection still manufacturing synthetic events

### July 15-16 — The Recovery

The recovery came from a methodical re-examination:

1. **`6b3efa3`** — "fix: stop main-agent warnings from leaking into worker panel" — Fixed the bridge's global bus handler to skip worker-sourced `token_warning` events (they were already forwarded via the per-worker bus)

2. **`c1c0fd4`** + **`5231594`** — Worker panel ctx header live updates + context freed notification — Properly wired `emit_context_updated` into `_run_tool_loop()`

3. **`6c3d3e3`** — "fix: wire WorkerBusAdapter.emit_context_updated into _run_tool_loop" — Finally connected the adapter method that had been defined but never called

4. **`1b0a506`** — "CRITICAL: fix worker crash — `self._bus` → `self._worker_bus_adapter`" — Fixed the reference that should have been updated when `WorkerBusAdapter` was introduced

5. **`81874a9`** — "fix: show real context_updated, suppress synthetic context_cleared" — Stopped manufacturing events; started trusting the real event pipeline

6. **`776677d`** — **HEAD**: "fix: align worker panel with core emissions" — Removed heuristics, fixed token wiring, dedup keys, and dual publish paths

### What Was Actually Accomplished

At HEAD, the system has:

- ✅ **WorkerBusAdapter** — A clean adapter class that forwards agent events via the per-worker EventBus
- ✅ **EventProcessor integration** — Worker agents now process events through the same pipeline as the main agent
- ✅ **Real `context_summarized` and `context_cleared` events** — These are now yielded by `process_query()` after summarization, not manufactured heuristically
- ✅ **`token_recovery` events** — The `_handle_state_event` method properly yields `token_recovery` events (previously swallowed)
- ✅ **Single dedup point** — The bridge's dedup for `context_updated` is based on formatted display strings, with frontend timestamp dedup as a secondary guard
- ✅ **Late-arriving bridge discovery** — Workers spawned before a bridge subscribes are discovered via `_discover_existing_workers()`
- ✅ **No heuristic summarization detection** — The ≥40% token drop heuristic is removed

---

## 2. Key Decision Points

### Decision 1: Refactor the Core Agent Loop

**What was decided:** To modify `agent/core/agent.py` to yield `context_summarized`, `context_cleared`, and `token_recovery` events from `_apply_summary_pruning()` and `_handle_state_event()`.

**Information available:** The `process_query()` generator yielded `token_update` and `token_warning` events, but system notifications (the `[SYSTEM NOTIFICATION]` message) were injected directly into `user_history` and never yielded as events. The analysis reports from July 9-10 had identified this gap.

**Alternatives:**
- **Alternative A:** Leave `process_query()` unchanged and have the worker/bridge layer parse `user_history` to detect summarization (the heuristic approach)
- **Alternative B:** Add event yielding in `process_query()` for the missing event types (the chosen path)
- **Alternative C:** Create a separate post-processing layer that reads `user_history` and emits events

**Why B was chosen:** The heuristic approach (A) was already proven unreliable — the ≥40% token drop heuristic fired on false positives and missed real events. Alternative C was unnecessary indirection. The cleanest fix was to have `process_query()` yield all events, making the event stream a complete representation of what happened.

**Consequences:**
- ✅ `context_summarized`, `context_cleared`, and `token_recovery` are now first-class events
- ✅ The worker's heuristic detection was removed
- ❌ The change required modifications to `_apply_summary_pruning()`'s return value (it now returns recovery events instead of None), which could affect callers
- ❌ The `handle_state_event` method grew more complexity

### Decision 2: Add `worker_state_sync`

**What was decided:** To create a synthetic `worker_state_sync` event that polls the agent's internal state and forwards it to the frontend.

**Information available:** The real-time token count updates weren't flowing through the worker path. The `context_updated` event was being yielded by `process_query()` but the worker's `_publish_event()` calls didn't forward it.

**Alternatives:**
- Fix the missing `context_updated` forwarding in `_run_tool_loop()`
- Use the existing `token_update` event and just forward it

**Why `worker_state_sync` was chosen:** At the time, the problem appeared to be that the frontend needed a comprehensive state snapshot (token count, warning state, critical threshold) in a single event, rather than multiple discrete events. The reasoning was that a single `worker_state_sync` event would be simpler for the frontend to handle than subscribing to `tokens_updated`, `context_updated`, `token_warning`, and `token_recovery` separately.

**Consequences:**
- ❌ Created a parallel state synchronization mechanism that competed with the real event types
- ❌ `worker_state_sync` was emitted after EVERY agent event, flooding the frontend
- ❌ The frontend needed special handling for `worker_state_sync` in the dedup, mapping, and rendering logic
- ❌ It was eventually retained as a supplementary sync mechanism even after the real events were fixed

### Decision 3: Add Dedup/Throttling Logic

**What was decided:** To add event-level dedup in the bridge (`_last_context_updated` map comparing formatted display strings) and timestamp-based dedup in the frontend (`makeDedupKey` comparing `(eventType, timestamp)` tuples).

**Information available:** The `context_updated` event was being emitted multiple times for the same logical state change. The frontend was receiving duplicate `worker:context_updated` events.

**Alternatives:**
- Find and fix the duplication source (dual publish paths: both `_run_tool_loop` and `EventProcessor` were emitting `context_updated`)
- Accept duplicates and let the frontend handle them (React state dedup by value)

**Why the chosen path was taken:** The duplication source wasn't immediately obvious. The two publish paths (manual `_publish_event` + `EventProcessor` flow) were refactored simultaneously, making it hard to isolate. Adding dedup was a surgical fix that didn't require untangling the whole pipeline.

**Consequences:**
- ❌ The bridge dedup key was a formatted display string (`"X.XK"`), not the raw token count — this meant different token values that formatted to the same display string were incorrectly deduplicated
- ❌ The frontend dedup key was `(eventType, timestamp)` — if two events had the same timestamp (possible with batch processing), only one would show
- ❌ Both dedup layers ran independently, sometimes with conflicting logic
- ✅ At HEAD, the bridge dedup is more conservative (skips only identical display strings) and the frontend dedup handles the dual-path normalization

### Decision 4: Hard-Reset `event_processor.py`

**What was decided:** To restructure `event_processor.py` from a monolithic processor to a dispatch-based processor with per-event-type handlers.

**Information available:** The original `EventProcessor` had a single `process_event()` method that routed to sub-processors, but the sub-processors had method signatures that didn't match the actual event data structures. The `_process_token_update_event` method had code that checked for `token_warning` and `turn_warning` inside it (copy-paste artifact).

**Alternatives:**
- Fix the method routing and signatures in-place
- Rewrite the processor with clean method signatures (the chosen path)

**Why the chosen path was taken:** The in-place fixes would have required touching every method. The class was small enough (~300 lines) that a rewrite was feasible and would produce cleaner code.

**Consequences:**
- ✅ Clean method signatures with correct parameter handling
- ✅ Added `_process_context_cleared_event` and `_process_token_recovery_event` handlers
- ❌ Did NOT add `emit_tokens_updated` or `emit_context_updated` wiring in the initial rewrite — these had to be added later

### Decision 5: Full Pipeline Audit

**What was decided:** To stop making incremental fixes and conduct a comprehensive audit of every event type across all pipeline layers.

**Information available:** Multiple symptoms were present:
- The worker panel showed stale data
- Events were duplicated
- System notifications were missing
- The heuristic summarization detection was unreliable

**Alternatives:**
- Continue patching individual symptoms
- Rebuild from scratch

**Why the chosen path was taken:** The audit was the methodical approach. The event system had grown organically and nobody had a complete mental model of all the paths. The audit produced a definitive map (see the `Event_Pipeline_Complete_Trace_Report.md` and `Full_Worker_Pipeline_Audit___Historical_Analysis.md`).

**Consequences:**
- ✅ Identified every event divergence between main agent and worker paths
- ✅ Documented every lost or manufactured event
- ✅ Provided a clear baseline for fixing the pipeline
- ✅ The audit itself didn't fix anything, but it enabled the fixes

### Decision 6: Phase 1 Fixes (July 15-16)

**What was decided:** To implement targeted fixes based on the audit findings, prioritizing:
1. Fix `return→continue` bug in `process_query()`
2. Wire `WorkerBusAdapter.emit_context_updated()` into `_run_tool_loop()`
3. Fix `self._bus` → `self._worker_bus_adapter` reference
4. Remove heuristic summarization detection
5. Suppress synthetic `context_cleared` events

**Information available:** The audit had identified exactly which code paths were wrong and what the correct behavior should be.

**Alternatives:**
- A comprehensive rewrite of the worker event pipeline
- A more conservative approach of leaving the heuristic in place as a fallback

**Why the chosen path was taken:** The audit made it clear which fixes were surgical and which were risky. Each fix addressed a specific divergence identified in the audit. The heuristic was removed because the real event types (`context_summarized`, `context_cleared`) now existed.

**Consequences:**
- ✅ All identified diveregences closed
- ✅ Worker panel now shows live data
- ✅ No manufactured events
- ❌ The `worker_state_sync` scaffolding remains (could be removed in a future cleanup)

---

## 3. Mistakes and Root Causes

### Category A: Scope Creep

**Pattern:** A display issue in the worker panel (e.g., "ctx: header not updating") was traced to a missing event in the worker pipeline, which was traced to a missing event type in `process_query()`, which was "fixed" by adding a new event type, which required updating the event processor, which required updating the bridge, which required updating the frontend — all for a header display fix.

**Examples:**
- The `context_updated` header fix touched 6 files across 4 layers (worker, events, bridge, frontend)
- The `worker_state_sync` event was created to forward token warnings, when the existing `token_warning` event just needed correct routing
- The heuristic summarization detection was a workaround for a missing event yield in `process_query()` — fixing the yield would have been simpler

**Root cause:** Each layer was treated as a black box. When a symptom appeared at layer N, the fix was applied at layer N (adding a workaround) rather than tracing back to the root cause at layer 1.

### Category B: Over-Engineering

**Pattern:** Building comprehensive solutions for simple problems.

| Problem | Simple Fix | What Was Built |
|---------|-----------|---------------|
| Duplicate `context_updated` events | Remove one of the two publish paths | Bridge-level dedup + frontend-level timestamp dedup |
| Token count not updating live | Forward the existing `token_update` event | `worker_state_sync` with per-event polling |
| Context summarization notification | Yield `context_summarized` from `process_query()` | Heuristic ≥40% token drop detection |
| Missing events for late-arriving bridge | Re-subscribe on connection | `_discover_existing_workers()` with full bus registry |

**Root cause:** AI agents tend to build complete solutions because they're cheaper than debugging. It's faster to write 200 lines of `WorkerBusAdapter` than to find and fix the bug in the existing 800-line `_run_tool_loop()`. The heuristic summarization detection was especially egregious — it was a machine learning approach to a programming problem.

### Category C: Corrupted Merges / Incomplete Testing

**Pattern:** Files corrupted during automated refactoring, leading to runtime errors that were treated as new bugs rather than merge artifacts.

**Examples:**
- `commit bdba5ca`: "Fix SyntaxError in agent/core/agent.py — corrupted duplicated blocks". The file had **duplicated code blocks** from a bad refactoring tool run.
- `EventProcessor._process_token_update_event`: Had `if event_type == 'token_warning':` inside it — a copy-paste artifact from when the method was copied from `process_event()`.
- `commit 26a3b53`: "broken code but need to go back" — a commit message that literally admits the code is broken.

**Root cause:** AI agents performing automated refactoring without verifying the output compiles or the tests pass. The `return→continue` bug in `process_query()` (line ~1200) was discovered only when the agent crashed at runtime — it should have been caught by a unit test or even an eyeball review.

### Category D: Loss of Methodical Discipline

**Pattern:** Claiming a fix was complete without live verification, then discovering the same symptom again.

**Examples:**
- The `context_updated` header fix was "done" three times before it actually worked:
  1. In `EventProcessor` integration (July 14) — but `emit_context_updated()` was never called
  2. In `WorkerBusAdapter` wiring (July 15) — but `self._worker_bus_adapter` vs `self._bus` typo
  3. Finally working at `6c3d3e3` (July 16)
- The heuristic summarization detection was "fixed" three times:
  1. In `EventProcessor` (added handler that was never triggered)
  2. In `forward_agent_event` (added `context_summarized` forwarding)
  3. Finally removed in `81874a9` (July 16)

**Root cause:** The fixes were made based on code reading, not runtime verification. The agent would trace through the code path in its head, determine it should work, and check the box. It would take a user report of "still broken" to trigger another look.

### Category E: Communication Breakdowns

**Pattern:** The user says "X is broken," the agent says "I fixed X," the user says "X is still broken," the agent says "Now I really fixed X," repeat.

**Examples:**
- The `ctx: header frozen` issue was reported, "fixed," reported again, "fixed" again, reported a third time, then finally actually fixed.
- The "⚠️ Token usage has returned to safe levels" message appearing in the worker panel was "fixed" three times before the root cause (dual publish path) was identified and addressed.

**Root cause:** The AI agent's model of the system was incomplete. It would trace through the code, find one issue, fix it, and assume the problem was solved — without considering that there might be multiple independent causes for the same symptom. The user, not having the code in front of them, would just keep reporting the same symptom.

---

## 4. What Saved Us

### Turning Point 1: The Full Stop

After the corrupted merge (`bdba5ca`) and the emergency commit (`26a3b53`: "broken code but need to go back"), the development paused. This was critical because:
- It stopped the cycle of hasty patches
- It forced a full inventory of what was broken
- It created space for the audit

**Lesson:** Sometimes the most productive thing is to stop and assess.

### Turning Point 2: The Comprehensive Audit

The decision to conduct a full pipeline audit (commits `a68383e` and the analysis reports of July 16) was the single most important decision. It:

1. **Mapped every event type** across every layer — from `process_query()` yield to frontend render
2. **Identified every divergence** between main agent and worker paths
3. **Documented every lost or manufactured event**
4. **Created a shared mental model** for subsequent fixes

The audit didn't fix anything, but without it, the fixes would have continued to miss the mark.

### Turning Point 3: The Single Source of Truth

The decision to make `context_updated` the canonical source for token count display (rather than `worker_state_sync`, `tokens_updated`, or heuristic detection) was critical. It meant:

- One event type to subscribe to
- One data format to parse
- One dedup point (the bridge's formatted string comparison)
- The frontend could be simplified to trust this one source

Similarly, removing the heuristic summarization detection and replacing it with real event types (`context_summarized`, `context_cleared`) eliminated an entire category of bugs.

### Turning Point 4: Mirroring the Main Agent's Mechanism

The `WorkerBusAdapter` + `EventProcessor` pattern was explicitly designed to mirror the main agent's `GUIIntegration` + `EventProcessor` pattern. This was the right architectural decision because:

- The main agent path was known-verified-working
- Any fix applied to the worker path could be validated against the main agent's behavior
- Future developers only need to learn one pipeline, not two

### Turning Point 5: Fixing `process_query()` at the Source

The decision to modify `_apply_summary_pruning()` to return recovery events, and to yield `context_summarized`/`context_cleared` from `process_query()`, was the correct root-cause fix. Instead of:

- Having the worker infer that summarization happened (heuristic)
- Having the bridge parse `user_history` to detect events
- Having the frontend guess at token state

...the `process_query()` generator now yields all events, making the event stream a complete, authoritative record.

---

## 5. Lessons for the User

### 5.1 How to Frame Tasks to Prevent Scope Creep

**Bad framing:** "The worker panel's context counter is frozen. Fix it."

This framing says "make this one thing work" without specifying how. The AI agent will trace the path from panel → frontend → bridge → worker → agent, find multiple gaps, and attempt to fix all of them — touching files across the entire stack.

**Better framing:** "The worker panel's context header shows '—' instead of the token count. Find out why `context_updated` events are not reaching the `WorkerOutputPanel` component. Fix only the missing link in the chain, then stop."

This framing narrows the scope to a single event type and a single missing link. It tells the agent to stop once the chain is complete, not to optimize or refactor the entire pipeline.

**Actionable rule:** Specify (a) the exact symptom, (b) the expected data flow, and (c) the stopping condition. Prefer "find the missing link" over "make it work."

### 5.2 When to Demand a Full Investigation

**Symptoms that warrant a full investigation:**
- The same bug is "fixed" twice
- A fix in one place causes a regression in another
- The AI agent says "I need to refactor X to fix Y"
- The AI agent proposes adding a new caching/dedup/throttling layer

**Recommended intervention:** "Stop. Don't fix anything yet. First, trace the complete data flow for event type X from source to display. Document every transformation, every routing decision, and every place the data could be lost or duplicated. Show me the trace before you write any code."

This forces the agent to:
1. Build a complete mental model of the system
2. Identify all the gaps before patching any of them
3. Present the analysis for review before committing to a fix

### 5.3 The Importance of a Single Verifiable Source of Truth

The biggest source of complexity in this period was **multiple overlapping mechanisms for the same data**:

- `context_updated` (the real event) competed with `worker_state_sync` (the synthetic event)
- `token_update` yielded from `process_query()` competed with heuristic token drop detection
- The real `[SYSTEM NOTIFICATION]` in `user_history` competed with the manufactured `"Context freed"` message

**Principle:** Every display element should have exactly one data source. If there are two paths for the same data, one is wrong — find and remove the wrong one, don't add dedup to handle both.

**Checklist for any new event type:**
1. Is it yielded by `process_query()`? (If not, it's not a real event.)
2. Is it forwarded through the bridge? (One path, not two.)
3. Does the frontend subscribe to exactly one event type for this data? (No dual handling.)

### 5.4 How to Recognize Core Bug vs. Scaffolding Bug

**Scaffolding bug symptoms:**
- A workaround exists for a known limitation (e.g., heuristic summarization detection)
- The fix is to add more code (a new dedup layer, a new event type, a new component)
- The bug appears in multiple unrelated places

**Core bug symptoms:**
- The fix is to remove or simplify code
- The bug has a single root cause (one wrong reference, one missing yield, one incorrect type)
- Fixing it eliminates multiple surface symptoms

**Rule of thumb:** If the fix involves adding more than 50 lines of new code, ask "am I building scaffolding for a core bug?" If the fix involves deleting code, ask "am I finally removing the scaffolding?"

### 5.5 The Value of "Build the Simplest Thing That Works and Then Stop"

**Anti-pattern:** "I'll add a comprehensive event routing system with dedup, throttling, state sync, and fallback detection, because the current approach has a gap."

**Pattern:** "The current approach has a gap at point X. I will connect point X to the existing pipeline. If the pipeline already handles format Y, I will make point X produce format Y. I will not add new formats, new event types, or new layers."

**Examples of over-engineering vs. simple fix:**

| Problem | Over-Engineered Fix | Simple Fix |
|---------|-------------------|-----------|
| `context_updated` not reaching frontend | Add `worker_state_sync` with per-event polling | Wire `emit_context_updated()` in `_run_tool_loop()` |
| Duplicate events | Bridge-level dedup + frontend timestamp dedup | Remove the second publish path |
| Missing `[SYSTEM NOTIFICATION]` | Heuristic token drop detection | Yield real events from `process_query()` |

---

## 6. Current System Health

### Status vs. One Week Ago

| Dimension | July 9 (Baseline) | July 16 (HEAD) | Assessment |
|-----------|------------------|-----------------|------------|
| Event completeness | Missing: `context_summarized`, `context_cleared`, `token_recovery` | All event types yielded by `process_query()` | ✅ Fixed |
| Worker pipeline | Manual `_publish_event()` calls, ad-hoc per event type | `WorkerBusAdapter` + `EventProcessor` integration | ✅ Fixed |
| Context summarization signal | Heuristic ≥40% token drop detection | Real `context_summarized` events from `process_query()` | ✅ Fixed |
| Token count display | `worker_state_sync` polling | `context_updated` via `WorkerBusAdapter.emit_context_updated()` | ✅ Fixed |
| `[SYSTEM NOTIFICATION]` rendering | Missing in worker panel | Rendered from `context_summarized` event | ✅ Fixed |
| Frontend handling | Manual dedup, duplicate rendering | Clean dedup with dual-path canonicalization | ⚠️ Improved |
| Core agent stability | `return` instead of `continue` | Fixed `return→continue` bug, post-summary survive | ✅ Fixed |
| Corrupted files | Duplicated blocks in `agent.py` | Clean file | ✅ Fixed |
| Bridge subscription race | Workers could start before bridge subscribed | `_discover_existing_workers()` with late-arriving guard | ✅ Fixed |
| Test coverage | No integration tests for event pipeline | Still minimal | ❌ Not improved |

### Residual Fragility

1. **`worker_state_sync` scaffolding remains.** The synthetic `worker_state_sync` event is still emitted after every agent event. It's no longer needed for token count updates (which come via `context_updated`), but removing it could break something that depends on it. Needs a cleanup pass.

2. **The bridge dedup uses formatted display strings.** `self._last_context_updated[worker_name]` stores `"X.XK"` — a formatted string. If a value changes from 5000 to 5100 (both "5.1K"), the dedup will incorrectly suppress the update. This should use raw token counts.

3. **EventProcessor has dead code.** The `_process_token_update_event` method still has the copy-paste code that checks for `token_warning` and `turn_warning`. This dead code doesn't cause bugs (the checks never match inside a token_update handler), but it's confusing.

4. **No pipeline integration tests.** The event pipeline has zero automated tests that verify end-to-end data flow. Every fix in this report was discovered manually or by users. A test that sends a fake `token_update` event through the pipeline and checks the frontend output would catch regressions instantly.

5. **The `_pending_warnings` buffer pattern is fragile.** The buffer in `_update_tokens_after_tool()` is flushed after `TurnTransaction.commit()` in `process_query()`. If a new code path calls `_update_tokens_after_tool()` without going through the flush logic, warnings are silently lost. This is already happening in the turn-warning path (which injects immediately) vs. the tool-result path (which buffers).

6. **The `_token_warning_has_fired` one-shot guard in `state.py`.** Once a warning fires, it won't fire again until `reset()` is called. If the conversation cycles LOW → WARNING → LOW → WARNING, the second warning is silently suppressed. This is a design flaw, not a bug — but it means the agent can hit the context window without a second notification.

### What to Fix Next (Priority Order)

1. **Remove `worker_state_sync`** — or at least stop emitting it on every event. Emit only on state transitions.
2. **Fix the bridge dedup** — use raw token counts, not formatted display strings.
3. **Clean up `EventProcessor._process_token_update_event()`** — remove dead code.
4. **Add pipeline integration tests** — one test per event type, from `process_query()` yield to frontend handler.
5. **Fix the `_token_warning_has_fired` guard** — make it resettable on LOW transitions.
6. **Document the `_pending_warnings` buffer pattern** — with a warning that new tool-calling code paths must flush the buffer.

---

## Appendix A: Event Flow Summary (HEAD State)

```
Agent.process_query()
  │
  ├─ token_update       → tokens_updated (main bridge) / WorkerBusAdapter.emit_tokens_updated()
  ├─ token_warning      → [SYSTEM NOTIFICATION] in user_history + yielded for event stream
  ├─ token_recovery     → yielded (new) → EventProcessor._process_token_recovery_event()
  ├─ context_cleared    → yielded (new) → EventProcessor._process_context_cleared_event()
  ├─ context_summarized → yielded (new) → WorkerBusAdapter.forward_agent_event()
  ├─ turn_warning       → [SYSTEM NOTIFICATION] in user_history + yielded
  ├─ time_warning       → [SYSTEM NOTIFICATION] in user_history + yielded
  ├─ agent_responded    → yielded → worker_message on per-worker bus
  ├─ tool_call          → yielded → WorkerBusAdapter → per-worker bus
  ├─ tool_result        → yielded → WorkerBusAdapter → per-worker bus
  ├─ turn               → yielded → assistant_message on per-worker bus
  ├─ error              → yielded → error on per-worker bus
  └─ stopped            → yielded → final

Bridge (per-worker bus handler):
  tokens_updated  → worker:tokens_updated (flattened to top-level keys)
  context_updated → worker:context_updated (with dedup by formatted display string)
  context_cleared → worker:context_cleared (with "Context freed" message)
  context_summarized → worker:context_summarized (with original message)
  All other types → worker:{type} (wrapped in event_dict)

Frontend (WorkerOutputPanel):
  Sets workerInfo from worker_state_sync (context_length, token_state, etc.)
  Maps events to display format via case/switch in incomingEvents handler
  Dedup via makeDedupKey(eventName, timestamp)
  Renders via MessageBubble component
```

---

*Report generated July 16, 2026. Based on analysis of ~200 commits, 6 key file diffs, comprehensive event pipeline audit, and knowledge base review.*
