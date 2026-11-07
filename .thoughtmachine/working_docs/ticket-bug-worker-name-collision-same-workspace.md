# bug/worker-name-collision-same-workspace

**Confirmed 2026-10-19 by field agent in fierce-owl-43:** two sessions in the
same workspace, both spawning worker named `default` with `force=True`, kill
each other's worker. Field agent worked around it by switching to `default2`.

**Open question:** does the same happen cross-workspace? Registry keying unknown.
If keyed by `(workspace_id, worker_name)`, no cross-workspace bug. If keyed by
`worker_name` alone, cross-workspace kill is live.

**Read first:**
1. Where is the worker registry? What's the key?
2. `WorkerThread.spawn(force=True)` — filter by workspace_id or not?
3. Test both scenarios.

**Fix shape:** registry keyed by `(workspace_id, worker_name)`. Same-workspace
same-name across sessions should either refuse or scope by `session_id` too.

**Filed by:** Gen 46, 2026-10-19.

## Sequential-session shadowing in `_worker_bus_subs` — **recorded, not fixed**

**Recorded 2026-10-22, NOT fixed** in `fix/worker-panel-payload-drop`: a second, narrower same-workspace symptom of the same session-blind identity, this one in the *bridge* rather than in the worker registry. `_worker_bus_subs` (`web_ui/backend/bridge.py:483`) is keyed by instance label only (`_worker_instance_label`, `:159-166`), never by session, so a bridge reused across two sessions in one workspace — `server.py:1703` and `:1763` rebind the same `WebAgentBridge` to a new session id with no `bridge.stop()` in between — carries session A's entry for worker `default` into session B. When session B spawns its own `default`, the payload-drop fix's re-bind logic cannot tell the two apart: the session guard at `bridge.py:836` compares the *event's* session id with `self._session_id`, and both are now session B, so it passes; `_worker_bus_is_stale` then resolves the registry using the **stored** session id, which is still A (`bridge.py:683` `session_id = entry.get('session_id') or self._session_id or ''`), and reports not-stale either way it can go — if session A's bus is still registered the object matches, and if it is gone `_stored_entry_is_stale` returns `False` by construction (`bridge.py:662-663`, `None` counts as "cannot tell, keep it"). The duplicate guard at `:853` therefore short-circuits `_on_worker_spawned` and returns: **no re-bind, no bounded retry, and no `worker:system_notification`** — the degradation notice only exists on the retry path, so it never fires — leaving session B's `default` silently attached to session A's dead bus. That is the same silent-loss shape as `ticket-bug-worker-panel-intermittent-missing-tool-calls.md`, one layer up: the worker panel shows nothing and nothing reports itself. *Simultaneous* sessions are **not** affected by this: nothing here is a singleton — each `WebAgentBridge` owns its own dict, the per-worker handlers built in `_make_handler` (`bridge.py:580-616`) are per-instance closures over `self`, and `create_bridge()` (`bridge.py:3055-3057`) is defined but never called — so two live sessions simply keep two independent maps. Candidate follow-up, **not addressed in this branch**: key `_worker_bus_subs` on `(session_id, label)`, or clear/re-scope it in `stop()`, in `close_session()` (`bridge.py:2532`, which reaches `:2579`) and on every server-side rebind path (`server.py:1703`, `:1763`).


## Correction (Gen 49)

**Correction (Gen 49, branch `fix/worker-panel-cross-session-unsubscribe`, dev `892d2780`).** The note above about a bridge being "reused across two sessions" needs one correction and one replacement. Correction: `_cached_bridge_for` is a plain `_session_bridges.get(session_id)` lookup at **`web_ui/backend/server.py:1075`** (not `:1086`), and every rebind path stops the old bridge and constructs a fresh `WebAgentBridge` (`server.py:3063-3075`); concurrent sessions never share a `_worker_bus_subs`. Replacement: the *cross-session* defect that actually bites in production is on the **unsubscribe** side, not the subscribe side. `global_event_bus` is process-global (`agent/events.py:645`), and `_on_worker_completed` / `_on_worker_error` (`web_ui/backend/bridge.py:1213-1244`) called `_unsubscribe_worker_bus(...)` unconditionally — the session guard lived only inside `_forward_worker_event` (`:1256-1261`), so it filtered the *forward* and nothing else, and `_unsubscribe_worker_bus` itself (`:1105-1150`) has no session check. A `WORKER_COMPLETED` for **another** session's worker labelled `default` therefore tore down *this* bridge's per-worker handlers, killing `tool_call_start` / `tool_call` / `tool_result` for the rest of that bridge's life while the separately-guarded status pings (`:836`) kept flowing — the precise "panel shows a worker that did nothing" shape. Fixed by hoisting the existing idiom into both handlers (`bridge.py:1225-1227`, `:1242-1244`); RED-first test `test_completed_event_from_foreign_session_preserves_subscriptions` in `tests/test_per_worker_eventbus_bridge.py`. Accepted trade-off: a rebind-stale entry (the note above) can no longer be reclaimed by a foreign completion, so the rebind path itself must clear `_worker_bus_subs` — that remains the recorded open work.
