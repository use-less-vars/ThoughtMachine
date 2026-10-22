# Ticket: bug/worker-panel-intermittent-missing-tool-calls

**FIXED on `fix/worker-panel-payload-drop`** — bridge side only; see `## Fix` and `## Test` below.

**Filed:** 2026-10-22, bookkeeping chunk B. Line numbers read from the working tree of `ThoughtMachine-dev`, dev `75f51e25`.
**Severity:** MEDIUM — no data is lost and nothing unsafe happens; a supervising human (or the main agent's own reading of the panel) is shown a worker that "did nothing" while it actually ran tools. Escalate to HIGH the moment the worker panel is load-bearing for supervising unattended workers: at that point the defect is *absence of observability that reports itself as success*, which is worse than a missing field (compare the sibling ticket below, where the field is always missing and therefore obviously missing).
**Status:** **fix implemented** on branch `fix/worker-panel-payload-drop` (cut from `cb5d2279`), bridge-side only, with the RED-first test evidence recorded in `## Test` below. **NOT pushed, NOT a PR, NOT merged.** Originally verified by read (static). **Not** deterministically reproducible from source: the defect is a *single-shot, silent, never-retried binding* between the bridge and the worker's per-worker event bus. Which is exactly why the report says "intermittent".
**Fix type:** backend robustness — verify/retry the per-worker bus binding, and make a failed binding visible to the frontend instead of two log lines. The primary cause needs **no frontend edit**.

## The defect

A worker's tool activity reaches the panel over a *second*, per-worker `EventBus` that the bridge must **bind to exactly once**, at spawn time, by looking the bus up in a process-global registry. If that single lookup misses, the entire detailed stream for that worker instance — `tool_call_start`, `tool_call`, `tool_result`, `worker_message`, `context_updated`, `token_warning`, … — is dropped for the lifetime of the bridge, silently. Nothing retries. Nothing tells the UI. The only trace is a `WARNING` in the server log.

The whole chain is intact *in principle*, which is why this was hard to see:

- `agent/core/agent.py:1287-1304` — the assistant message is committed (`:1294-1295` `commit_assistant_only()`) and the `turn_event` is built at `:1298` with `'tool_calls': []` — **emptied** — then yielded at `:1304`.
- `agent/core/agent.py:1305-1322` — the pre-execution `tool_call_start` events are built at `:1314-1320` (`{'type': 'tool_call_start', 'tool_name', 'arguments', 'tool_call_id', 'turn'}`) and yielded at `:1322`.
- `agent/core/agent.py:1323` — `execute_tool_calls(...)` runs **synchronously**; `:1336-1337` commits the turn; `:1338-1341` then yields all `tool_call` events and `:1342-1345` all `tool_result` events, **back to back, after the tools have already finished**.
- `agent/events.py:31` — `TOOL_CALL_START = 'tool_call_start'` exists in the enum, so the adapter's `EventType(event_type)` conversion at `worker_thread.py:582` does not raise for it.
- `tools/workspace/worker_thread.py:443-454 / :456-463 / :465-476` — `WorkerBusAdapter.forward_agent_event` maps the three types onto `_publish("tool_call_start" | "tool_call" | "tool_result", {...})`.
- `tools/workspace/worker_thread.py:569-593` — `_publish` builds `BaseEvent(type=EventType(event_type), metadata=EventMetadata(source="worker", session_id=""), data={**data, "worker_name": self.worker_name})` and calls `self._event_bus.publish(event)`.
- `web_ui/backend/bridge.py:840-846` — the bridge's `subscribed_types` list *does* include `'tool_call', 'tool_call_start', 'tool_result'`.
- `web_ui/frontend/src/components/WorkerOutputPanel.jsx:334-369` — the panel *does* have render cases for all three.

So the loss is not in the mapping. It is in the **binding**, and the binding has exactly two chances, both of which give up permanently:

1. **Spawn time.** `web_ui/backend/bridge.py:744-800` (`_on_worker_spawned`). After the session guard (`:768-771`) and the duplicate guard (`:775-779`), it resolves the bus at `:784`:
   `worker_bus = get_worker_event_bus(session_id, worker_name, instance_id=instance_id)`.
   If that returns `None` (`:785`), the whole `else` branch fires a `WARNING` at `:791-800` whose text is the diagnosis verbatim: *"detailed events (tool_call, tool_result, assistant_message) will NOT be forwarded to the frontend … This may be a race condition: worker bus registered before bridge subscribes."* — and then **returns without a retry, a backoff, or a frontend-visible signal**.
2. **Bridge construction time.** `web_ui/backend/bridge.py:631-635` calls `_discover_existing_workers(self._session_id)` once, which enumerates `WorkerRegistry.get_event_buses_for_session()` (`:648`) to catch workers spawned before the bridge existed (second tab, reconnect). This is the only later recovery, and it runs **once per bridge object**, not per WebSocket connect.

Both chances are gated by the same `in self._worker_bus_subs` test — `:775-779` and `:650-653` — and that is where the "intermittent" lives, because `_worker_bus_subs` can hold an entry that points at a **dead** bus.

**The registry key is a strict 3-tuple**, so the lookup is exact-or-nothing: `tools/workspace/worker_registry.py:151-170`

```
key = (session_id or "", worker_name, instance_id)
```

Registration happens at `tools/workspace/worker_thread.py:2266`
`register_worker_event_bus(self.session_id or "", self.worker_name, self._event_bus, instance_id=self.instance_id)`
and the spawn event is published *after* it, in the same thread (`:2276-2292`, carrying `"session_id"`, `"instance_id"`, `"instance_label"`). So the in-process ordering is correct. The bridge's side of the key, however, is reconstructed from the payload — `:757` `instance_id = data.get('instance_id') or 1` and `:759` `session_id = data.get('session_id', self._session_id or '')`. Any disagreement between that reconstruction and `self.session_id` / `self.instance_id` at registration turns the lookup into a permanent `None`.

**Ranked candidate mechanisms** (static reading; each needs one runtime log to confirm — the `WARNING` at `:791-800` is the discriminator, and it is already greppable):

1. **Entry removed from the registry while the bridge still believes it is subscribed.** The bus is unregistered in a `finally` at `tools/workspace/worker_thread.py:2956-2959`, but the bridge only learns about it through the *global* `WORKER_COMPLETED` / `WORKER_ERROR` publishes (`:2918-2938` and `:2889-2910`), each wrapped in `try/except: pass`. If that publish is swallowed, or the thread ends through a path that does not publish (a `BaseException`, a force-stop that pops the registry entry first — see the note at `:2947`), the registry entry disappears while `bridge._worker_bus_subs[worker_label]` stays. The next worker instance that reuses that label is then skipped at `:775-779` **and** at `:650-653`, and its detailed stream is lost for the rest of the bridge's life. This is the mechanism that best matches "sometimes, and then it keeps happening for that worker".
2. **Instance-label/instance-id disagreement.** `_worker_instance_label` (`bridge.py:158-165`) maps `None`/`1` → bare name, `N` → `name#N`; `_worker_instance_parts` (`:168-178`) inverts it. The registry keys the *bus* by raw `(session_id, worker_name, instance_id)` while the bridge keys its *subscription dict* by the derived label. A worker spawned at `instance_id=2` whose event payload omits `instance_id` resolves to `1` at `:757` → lookup hits instance 1's slot (or nothing) → `None` → warning → loss for instance N only, while instance 1 looks fine.
3. **Session id disagreement.** `session_id` at `:759` falls back to `self._session_id or ''`. A bridge whose `_session_id` is not yet set (or is set to a different normalisation of the same session) registers nothing, and `_discover_existing_workers` (`:634-635`) also ran with the empty value, so nothing is ever discovered. Both are silent.

**Secondary, adjacent silent-loss points in the same path** (not the primary cause; do not conflate):

- `agent/events.py:471-485` — `EventBus.publish` copies the subscriber list under lock and invokes each callback inside `try/except`, logging and continuing: a raising subscriber loses *its* events only.
- `tools/workspace/worker_thread.py:592-593` — `_publish` swallows every exception into one `WARNING`.
- `tools/workspace/worker_thread.py:574-579` — `_publish` returns early, with only a `DEBUG` line, when `self._event_bus is None`. The lazy init at `:1834-1839` logs `event_bus={'SET' if self._event_bus else 'NONE'}` — i.e. the code itself anticipates constructing `WorkerBusAdapter(self._event_bus, …)` with `None`. A `None` bus means every `tool_*` publish is a no-op.
- `web_ui/backend/bridge.py:437-439` and `:1165-1175` — `_worker_event_buffer` is capped at 100 with `pop(0)`; `:939-945` buffers every non-heartbeat event while `not self._forwarder._callbacks`. A tool-heavy turn during an F5 gap can evict earlier `tool_call_start` rows before the socket returns. The replay path exists, but it is lossy and bounded.
- `web_ui/backend/bridge.py:1002-1023` — `_unsubscribe_worker_bus` re-resolves the bus from `self._session_id or ''`, *not* from the payload session id used at subscribe time. When they differ, the lookup returns `None`, the `worker_bus.unsubscribe(...)` call is skipped by the `if worker_bus is not None` guard, and the subscription objects stay attached to a still-live bus (duplicates, not losses) while the dict entry is popped.

**Explicitly excluded** (so a fix is not sent to the wrong file):

- **Frontend dedup is not the cause.** `WorkerOutputPanel.jsx:184-209` builds the key as `canonical(eventName) + '|' + (timestamp || '')`; `canonical()` only folds `worker_message` / `final_response` / `assistant_message` → `final_response` and `token_warning` / `system_notification` → `system_notification`. `tool_call_start`, `tool_call` and `tool_result` pass through unchanged, so they can never collide with each other. The timestamp is `EventMetadata.timestamp = Field(default_factory=datetime.now)` (`agent/events.py:99-105`), built at `_publish` time per event, so it is distinct per event. The bailout at `:517-520` only skips events whose key is already in `seenEventKeysRef` (`:179`).
- **The renderer is not the cause.** `:334-352` handles `tool_call_start` (creates a `running` request row), `:353-363` `tool_call`, `:364-369` `tool_result`.
- **The panel no longer polls**, so nothing "refreshes" a lost subscription away: the WS-only effect is at `:213`, with the comments at `:157` / `:175` stating it. `WorkerManagementPanel.jsx` still polls `/api/workspace/<ws>/workers/active` every 3000 ms, but that endpoint serves the *worker list*, not events, and `current_task` from it is always `undefined` (see the sibling ticket).
- **The `worker_name` filter is not the cause.** `WorkerOutputPanel.jsx:223` requires `e.worker_name === workerName`; `_publish` unconditionally injects `worker_name` (`worker_thread.py:586`) and the bridge copies `data.get('worker_name', worker_label)` (`bridge.py:924`). The instance filter (`:227-235`) compares `instance_id` when both sides have it and otherwise `instance_label`, whose fallback is the subscription label (`bridge.py:925-926`) — consistent with `_worker_instance_label`.

## How it was found

Chasing the report "the worker panel sometimes shows no tool calls" hop by hop, the way an unrelated investigation does: start at the renderer, walk backwards, and check every hop for a place where an event can be dropped without a trace. The renderer, the dedup, the adapter mapping and the agent's event emission all survived that walk (evidence above). The bridge's subscription handshake did not: it is the only hop in the chain that is *conditional* and *never re-attempted*, and it is the only hop whose failure mode is a free-text `WARNING` rather than a missing field or an exception.

The correction worth recording: the briefing hypothesis that `WORKER_COMPLETED` / `WORKER_ERROR` may fail to unmount the subscription was **partly wrong** — they do. `bridge.py:1027-1038` (`_on_worker_completed`) and `:1040-1051` (`_on_worker_error`) both call `_unsubscribe_worker_bus(...)` unconditionally after forwarding, and `:716-740` (`_unsubscribe_worker_events`) tears down everything. So the stale-subscription trap is real but requires the *terminal global event to be missed*, which narrows the primary suspicion to the swallowed publishes at `worker_thread.py:2939-2940` / `:2910-2911` and to abnormal thread exits, rather than to the ordinary completion path.

## Why it matters

The panel is the only surface where a human sees *what* a worker did. A missing tool row is indistinguishable from a worker that reasoned for a minute and then answered — and the same panel is the surface used to decide whether to let workers run unattended. Every other observability surface is intact (`status.json` at `worker_thread.py:3135-3156`, `/workers` list, the WS `worker_status` events), so the artifact is specifically "the one that a supervisor watches". The defect also poisons diagnosis of *other* bugs: two separate reports in this wave ("worker did nothing", "worker panel empty") resolve to this binding rather than to the worker.

## Fix

Implemented in `web_ui/backend/bridge.py` only — **the writer (`tools/workspace/worker_thread.py`) was NOT changed and the frontend was NOT changed.** What the fix actually does, in the same spirit as the plan it replaces:

1. **The bus object is now stored, not just the subscriptions.** `_worker_bus_subs` changed value shape from `label -> {event_type: (event_type, handler)}` to `label -> {'bus', 'session_id', 'instance_id', 'subs'}`; `_subscribe_to_worker_bus` grew optional `session_id` / `instance_id` arguments (defaulted to `self._session_id or ''` and `_worker_instance_parts(label)[1]`, so existing callers keep working) and writes that entry. Unsubscription therefore has the object it needs without a registry round-trip.
2. **Staleness is a positive object mismatch, nothing weaker.** `_stored_entry_is_stale(entry, current_bus)` (stale only when `current_bus is not entry['bus']`) and `_worker_bus_is_stale(label)` (which re-resolves from the *stored* `session_id` / `instance_id`) decide it. `None` — registry empty, lookup raising, `WORKER_BUS_AVAILABLE` false — deliberately counts as **NOT stale**, so a live subscription is never torn down on a transient miss. The accepted price: a registry entry that vanishes on its own cannot invalidate ours (see the still-open note on terminal publishes below).
3. **The duplicate guards now re-bind instead of skipping.** In `_on_worker_spawned` (`:775-779`) the staleness check runs *before* the `in self._worker_bus_subs` test; in `_discover_existing_workers` (`:650-653`) the stored entry is compared against the bus the registry iteration just handed over. Stale → `_unsubscribe_worker_bus(...)`, then subscribe the bus that is current now. This is mechanism (1) closed: a respawned instance reusing a label is bound again instead of being skipped for the rest of the bridge's life.
4. **`_unsubscribe_worker_bus` unsubscribes from the STORED bus.** This was the asymmetry recorded above under "secondary" (`:1002-1025`): subscribing used the payload session id while unsubscribing re-resolved from `self._session_id or ''`, so a mismatch skipped every `unsubscribe` behind an `if worker_bus is not None` guard — a dead bus kept our handlers, or a live one collected duplicates. It now walks `entry['subs']` against `entry['bus']`; an entry without a stored bus (legacy shape) falls back to the old best-effort lookup, so the change is backwards compatible. Side effect worth noting: `_on_worker_completed` / `_on_worker_error` (`:1027-1051`, MUST NOT TOUCH) keep calling it unconditionally and now detach from the correct bus.
5. **Resolution is retried, and the bound is a stated choice.** `_resolve_worker_bus_with_retry(session_id, worker_name, instance_id)` tries `_WORKER_BUS_RESOLVE_ATTEMPTS = 5` times with `_WORKER_BUS_RESOLVE_DELAY = 0.25` s between attempts — 5 attempts × 250 ms = 1.5 s worst case, under 1.25 s of it spent sleeping — and both constants carry a comment naming the bound as deliberate: this runs on the event-bus publishing thread and must never block it indefinitely for a worker whose bus genuinely never appears.
6. **A failed binding is now visible to the user, exactly once.** On final failure `_notify_worker_events_unavailable(worker_name, instance_id, worker_label, session_id)` emits one `worker:system_notification` whose `response.message` names the worker label, reusing the existing envelope (no new event type, no new field) so `WorkerOutputPanel.jsx` renders it without a frontend change. It is buffered via `_buffer_worker_event` **before** the no-callback gate, so a degradation seen during an F5 gap is replayed rather than lost, and the two original `WARNING` lines at `:791-800` are kept verbatim so the log discriminator stays greppable.

Still open from the plan this replaced, and deliberately **not** implemented here: re-running discovery on WebSocket connect (old point 3), carrying `session_id` / `instance_id` / `instance_label` on every per-worker event (old point 4 — a writer change), and promoting the swallowed terminal publishes to `WARNING` (old point 5 — also a writer change). This branch is the bridge side only.

**Recorded, NOT fixed here** — two additions surfaced by the operator while reviewing this fix. Neither is a regression of it, and neither belongs in this branch:

- **`worker_thread.py:456-463` — `tool_call` omits `tool_call_id`.** The pre-execution `tool_call_start` at `:453` carries a `tool_call_id`; the `tool_call` row published at `:456-463` does not, so a `tool_call` row cannot be correlated with its own start row. Widening that payload means editing `web_ui/frontend/docs/toolcalls_streaming_backend_contract.md` *with* it (see MUST NOT TOUCH), so it is a writer+contract change, not a bridge one. **NOT fixed in this branch.**
- **`worker_thread.py:350-369` (`WorkerBusAdapter.state`, publish at `:367`) does publish `WORKER_STATUS` on the per-worker bus, and the bridge silently discards it** because `bridge.py:840-846` `subscribed_types` does not include `worker_status`. Harmless today — status is carried on the global bus, which is what the panel consumes — and therefore **recorded so nobody "fixes" it in isolation**: adding `worker_status` to `subscribed_types` without removing the global path would duplicate every status row.

Adjacent, same file, *separate* fix — record but do not bundle:

- `worker_thread.py:460-463` drops the `tool_call_id` that `agent/core/agent.py:1320` supplies. The panel therefore cannot correlate a `tool_call_start` row with its `tool_call` / `tool_result`, and an orphaned row is indistinguishable from a lost event. `web_ui/frontend/docs/toolcalls_streaming_backend_contract.md` documents the omission as the current contract; widening it means editing the contract too.
- `agent/core/agent.py:1323-1345` emits `tool_call` / `tool_result` only after `execute_tool_calls` has returned, so for the entire duration of a slow tool the panel has only the `tool_call_start` row. Some "missing tool call" reports are this, not a loss.
- **The global-bus unsubscribe handles are always `None` (surfaced while writing this fix; needs its own ticket, NOT fixed here).** `EventBus.subscribe()` returns `None` (`agent/events.py:455`), so every global subscription handle in `WebAgentBridge` — `_worker_spawned_sub`, `_worker_status_sub`, `_worker_completed_sub`, `_worker_error_sub`, `_worker_partial_result_sub`, `_worker_token_warning_sub`, `_worker_message_sub` — is always `None`, and `_unsubscribe_worker_events()` (`bridge.py:716-740`) can therefore never detach any of them. Even if a handle were real, that method passes it to `global_event_bus.unsubscribe(sub)` with **one** argument, while the signature is `unsubscribe(event_type, callback)` (`agent/events.py:492`). Consequence: each `WebAgentBridge` instance leaves permanent listeners on the global bus — a leak that grows per tab / per reconnect. It is a separate defect with a separate blast radius (leaked listeners vs. dropped worker events), so it is **recorded, not bundled**. The three new tests work around it by using a unique session id per test, which makes the leaked listeners inert via the `:768-771` session guard; any test that reused a session id would see cross-talk from earlier bridges.

Filed as its own ticket, since it needs its own fix and its own test: `.thoughtmachine/working_docs/ticket-hygiene-eventbus-subscribe-returns-none-bridge-leaks-listeners.md` (**recorded 2026-10-22, not fixed**). One correction to the bullet above, for whoever works that ticket: the one-argument `unsubscribe(sub)` call is never actually reached, because the `if sub is not None:` guard at `bridge.py:797` skips it — the handles being `None` is what makes the leak silent, not an exception — and the count is **eight** handles, not seven, since `_security_subscription` (`bridge.py:562-564`, detached by `_unsubscribe_security_events` at `:772-782`) leaks on exactly the same mechanism. It was deliberately left out of this branch because the clean fix is a call-site change to how *this* bridge records its global subscriptions (store `(event_type, callback)` pairs and use the existing two-argument `unsubscribe`), which rewrites the teardown (`stop()` → `_unsubscribe_worker_events()`) that the payload-drop fix deliberately did not touch, and the alternative — giving `EventBus.subscribe()` a return value — is an `agent/events.py` API change with a process-wide blast radius. The symptom is already documented as a workaround in `tests/test_worker_bus_respawn_bridge.py:45-56`, where the per-test unique `session_id` exists only to keep the leaked listeners inert.

## Test

RED-first, run against the working tree in the dev container — Python 3.12.15, pytest 9.1.1, `python3 -m pytest` from `/workspace`. **This was not a CI run**, and nothing is committed or pushed.

New file `tests/test_worker_bus_respawn_bridge.py`, three tests:

- `test_respawn_after_bus_replacement_still_delivers_tool_events` — subscribe on bus A, replace the registry entry with bus B under the same key, re-publish `WORKER_SPAWNED`, then assert that a `tool_call_start` published on **B** is forwarded *and* that A no longer delivers. The second half is what proves the stored-bus unsubscribe, i.e. that mechanism (1) is actually closed rather than papered over.
- `test_missing_worker_bus_retries_boundedly_then_degrades_once` — `get_worker_event_bus` patched to a recording `None`; asserts exactly 5 resolution attempts, no `_worker_bus_subs` entry afterwards, and exactly one `worker:system_notification` naming the worker (`worker_name` / `instance_id` / `instance_label` present, message containing the label).
- `test_stop_and_respawn_without_completion_leaves_no_dead_bus_bound` — the "intermittent" regression: unregister bus A **without** publishing `WORKER_COMPLETED`, register bus B under the same key, publish `WORKER_SPAWNED`, assert B delivers tool events and A stays silent.

All three were written **before** the fix and all three failed against unmodified `bridge.py` (`3 failed in 1.48s`), each at its intended assertion:

```
E   AssertionError: tool_call_start published on the worker's CURRENT bus was not forwarded
                   after respawn — the bridge is still bound to the dead bus; received=[]
E   AssertionError: expected exactly 5 bounded resolution attempts, got 1
E   AssertionError: the respawned worker's tool_call_start was not forwarded — the bridge is
                   still bound to the dead bus; received=[]
```

After the fix, same command: `3 passed in 3.14s`.

Regression: the touched suites (`test_worker_bus_respawn_bridge`, `test_per_worker_eventbus_bridge`, `test_worker_instance_ui`, `test_worker_container_bridge`, `test_bridge_normalize_for_frontend`, `test_bridge_permissions_sync`, `test_bridge_continue_session_config_log`, `test_bridge_load_session_workspace_scope`, `integration/test_bridge_error_notice_dedup`, `presenter/test_context_updated_bridge`, `web_ui/backend/tests/test_bridge_dedup`) → `80 passed`. Broader sweep `-k "bridge or worker or event_bus or eventbus"` over `tests web_ui/backend/tests` → `774 passed, 5 skipped, 4524 deselected, 4 xfailed, 12 subtests passed in 30.80s`.

From the test plan this supersedes, two cases are **not** covered: the liveness case (a subscriber raising inside `WORKER_COMPLETED` must still leave `_worker_bus_subs` empty — the unsubscribe is unconditional and unchanged) and the frontend manual check (no frontend change is in this branch, so the panel behaviour it would exercise is unchanged).

One note for whoever extends these: `tests/test_per_worker_eventbus_bridge.py` currently `print()`s a `WARNING` instead of asserting. It was left as-is — do not weaken it, but strengthening it is a candidate follow-up.

## Bearing

- **Always-missing sibling, do not re-file:** `.thoughtmachine/working_docs/ticket-bug-worker-context-not-shown-in-panel.md`. Same feature, different failure shape: there, `_worker_active_entry` (`web_ui/backend/workspace_routes.py:510-529`) returns exactly eight keys and never `current_task`, so the symptom is *stable* and always visible; here the field is present in the contract and the *delivery* is unreliable. That ticket also names a secondary race — `worker_thread.py:2541-2543` publishes the busy status before `self.current_task = query[:200]` at `:1817` — which is a third, unrelated way to see "the panel does not know what the worker is doing". A fix here must not be credited to either of those.
- Related by mechanism (silent, single-shot, log-only failure), not by code path: `ticket-bug-live-view-container-stale-no-push.md`, `ticket-bug-panel-actions-no-op.md`.
- The pipeline is heavily `log('DEBUG', 'pipeline.hops', …)`-instrumented (`bridge.py:856-861`, `:946-956`; `worker_thread.py:418-421`, `:570-573`, `:589-591`). Any confirmation or refutation of the ranking above should come from `pipeline.hops` + `server.bridge` logs, **not** from the UI.

## MUST NOT TOUCH

- `tools/workspace/worker_registry.py:151-170` — the `(session_id, worker_name, instance_id)` key. Changing its shape silently invalidates every producer and consumer of the registry; if the identity scheme changes it changes in one place, deliberately.
- `web_ui/backend/bridge.py:1027-1051` — `_on_worker_completed` / `_on_worker_error` unsubscribe unconditionally and must keep doing so even when `_forward_worker_event` decides not to forward (session mismatch). Moving the unsubscribe behind the forwarding guard reintroduces the stale entry by construction.
- `agent/core/agent.py:1287-1345` — the turn/tool event ordering and `commit_assistant_only()`. The event *shape* is the contract for both the adapter and the panel.
- `web_ui/frontend/src/components/WorkerOutputPanel.jsx:184-209` — the dedup key. Widening `canonical()` to fold the `tool_*` types would merge genuinely distinct rows and hide results.
- `web_ui/frontend/docs/toolcalls_streaming_backend_contract.md` — the contract document; edit it *with* the payload change it describes, never ahead of it.
